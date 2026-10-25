//! Explicit development build/package command. Installation never executes it.
use sha2::{Digest, Sha256};
use std::{
    error::Error,
    fs,
    path::{Path, PathBuf},
    process::Command,
};
fn package() -> Result<(), Box<dyn Error>> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() != 1 {
        return Err(
            "usage: cargo run --locked --bin package -- /absolute/new/dist-directory".into(),
        );
    }
    let destination = PathBuf::from(&args[0]);
    if !destination.is_absolute() || destination.exists() {
        return Err("destination must be absolute and must not already exist".into());
    }
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let parent = destination
        .parent()
        .ok_or("destination needs a parent")?
        .canonicalize()?;
    let resolved = parent.join(destination.file_name().ok_or("destination needs a name")?);
    if resolved.starts_with(root.canonicalize()?) && !resolved.starts_with(root.join("dist")) {
        return Err("in-checkout output is permitted only under ignored dist/".into());
    }
    let destination = resolved;
    let target = std::env::var_os("CARGO_TARGET_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join("target"));
    let target = if target.is_absolute() {
        target
    } else {
        root.join(target)
    };
    let status = Command::new(env!("CARGO"))
        .current_dir(root)
        .env("CARGO_TARGET_DIR", &target)
        .args([
            "build",
            "--release",
            "--locked",
            "--no-default-features",
            "-p",
            "ahu-github-cli-plugin",
            "-p",
            "ahu-github-tracker-plugin",
        ])
        .status()?;
    if !status.success() {
        return Err("release build failed".into());
    }
    fs::create_dir(&destination)?;
    let mut hashes = String::new();
    for (id, source, binary) in [
        ("github-cli", "package", "ahu-github-cli-plugin"),
        (
            "github-tracker",
            "tracker/package",
            "ahu-github-tracker-plugin",
        ),
    ] {
        let package = destination.join(id);
        fs::create_dir_all(package.join("bin"))?;
        let executable = target.join("release").join(binary);
        fs::copy(&executable, package.join("bin/entrypoint"))?;
        fs::copy(
            root.join(source).join("ahu-plugin.md"),
            package.join("ahu-plugin.md"),
        )?;
        // This is package identity metadata, not an installation-time build recipe.
        let manifest = fs::read_to_string(root.join(source).join("Cargo.toml"))?;
        let identity = manifest
            .split("[dependencies]")
            .next()
            .ok_or("missing package identity")?;
        fs::write(package.join("Cargo.toml"), identity)?;
        for document in ["LICENSE", "README.md", "SECURITY.md"] {
            fs::copy(root.join(document), package.join(document))?;
        }
        let bytes = fs::read(executable)?;
        hashes.push_str(&format!(
            "{:x}  {id}/bin/entrypoint\n",
            Sha256::digest(bytes)
        ));
        println!("{}", package.display());
    }
    fs::write(destination.join("SHA256SUMS"), hashes)?;
    Ok(())
}
fn main() {
    if let Err(error) = package() {
        eprintln!("package: {error}");
        std::process::exit(1);
    }
}
