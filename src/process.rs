//! Bounded Unix subprocess transport. Authentication stays with native gh.
use crate::{Error, LIMIT};
use std::{
    ffi::CStr,
    io::Read,
    os::{
        fd::AsRawFd,
        unix::{fs::PermissionsExt, process::CommandExt},
    },
    path::PathBuf,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

pub(crate) struct Process {
    pub(crate) executable: Option<PathBuf>,
    pub(crate) home: PathBuf,
    pub(crate) deadline: Instant,
    pub(crate) timeout: Duration,
}
struct OwnedChild(Child);
impl Drop for OwnedChild {
    fn drop(&mut self) {
        // SAFETY: the child creates its own process group before exec; its PID
        // identifies that group. Always kill descendants, even after leader exit.
        unsafe {
            libc::kill(-(self.0.id() as i32), libc::SIGKILL);
        }
        let _ = self.0.wait();
    }
}
fn home() -> Result<PathBuf, Error> {
    let mut buffer = vec![0u8; 65536];
    let mut entry = std::mem::MaybeUninit::<libc::passwd>::uninit();
    let mut result = std::ptr::null_mut();
    // SAFETY: writable buffers live through the call; returned strings are
    // copied before the backing buffer is dropped. No global passwd storage.
    unsafe {
        if libc::getpwuid_r(
            libc::getuid(),
            entry.as_mut_ptr(),
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            &mut result,
        ) != 0
            || result.is_null()
        {
            return Err(Error::Unavailable);
        }
        let entry = entry.assume_init();
        if entry.pw_dir.is_null() {
            return Err(Error::Unavailable);
        }
        use std::os::unix::ffi::OsStrExt;
        let path = PathBuf::from(std::ffi::OsStr::from_bytes(
            CStr::from_ptr(entry.pw_dir).to_bytes(),
        ));
        if !path.is_absolute() {
            return Err(Error::Unavailable);
        }
        Ok(path)
    }
}
impl Process {
    pub(crate) fn native(tracker: bool) -> Result<Self, Error> {
        let home = home()?;
        let mut paths: Vec<PathBuf> = [
            "/opt/homebrew/bin/gh",
            "/usr/local/bin/gh",
            "/usr/bin/gh",
            "/bin/gh",
            "/home/linuxbrew/.linuxbrew/bin/gh",
        ]
        .into_iter()
        .map(Into::into)
        .collect();
        paths.extend([home.join(".local/bin/gh"), home.join("bin/gh")]);
        let executable = paths.into_iter().find(|p| {
            p.metadata()
                .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        });
        Ok(Self {
            executable,
            home,
            deadline: Instant::now() + Duration::from_secs(if tracker { 26 } else { 12 }),
            timeout: Duration::from_secs(if tracker { 20 } else { 12 }),
        })
    }
    pub(crate) fn run(&mut self, args: &[String], mutation: bool) -> Result<Vec<u8>, Error> {
        let failure = if mutation {
            Error::Unknown
        } else {
            Error::Failed
        };
        let deadline = self.deadline.min(Instant::now() + self.timeout);
        if Instant::now() >= deadline {
            return Err(failure);
        }
        let executable = self.executable.as_ref().ok_or(Error::Unavailable)?;
        let child = Command::new(executable)
            .args(args)
            .current_dir(&self.home)
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("HOME", &self.home)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .process_group(0)
            .spawn()
            .map_err(|_| Error::Unavailable)?;
        let mut child = OwnedChild(child);
        let mut stdout = child.0.stdout.take().ok_or(failure)?;
        let mut output = Vec::new();
        let mut chunk = [0u8; 8192];
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(failure);
            }
            let mut descriptor = libc::pollfd {
                fd: stdout.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            };
            // SAFETY: descriptor is a valid writable pollfd and stdout stays open.
            let status = unsafe {
                libc::poll(
                    &mut descriptor,
                    1,
                    remaining.as_millis().min(i32::MAX as u128).max(1) as i32,
                )
            };
            if status < 0
                && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted
            {
                continue;
            }
            if status <= 0 {
                return Err(failure);
            }
            let count = match stdout.read(&mut chunk) {
                Ok(n) => n,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => return Err(failure),
            };
            if count == 0 {
                break;
            }
            if output.len() + count > LIMIT {
                return Err(failure);
            }
            output.extend_from_slice(&chunk[..count]);
        }
        loop {
            match child.0.try_wait().map_err(|_| failure)? {
                Some(status) => {
                    return if status.success() {
                        Ok(output)
                    } else {
                        Err(failure)
                    };
                }
                None if Instant::now() >= deadline => return Err(failure),
                None => std::thread::sleep(Duration::from_millis(2)),
            }
        }
    }
}
