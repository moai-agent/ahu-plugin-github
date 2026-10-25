//! Strict JSON, including duplicate keys nested inside otherwise unused fields.
use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Number, Value};
use std::fmt;

pub(crate) struct Unique(pub Value);
impl<'de> Deserialize<'de> for Unique {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Strict;
        impl<'de> Visitor<'de> for Strict {
            type Value = Unique;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("unique-key JSON")
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> Result<Unique, E> {
                Number::from_f64(v)
                    .map(|n| Unique(Value::Number(n)))
                    .ok_or_else(|| E::custom("nonfinite"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_string<E: de::Error>(self, v: String) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_unit<E: de::Error>(self) -> Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Unique, A::Error> {
                let mut values = Vec::new();
                while let Some(Unique(v)) = seq.next_element()? {
                    values.push(v);
                }
                Ok(Unique(Value::Array(values)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Unique, A::Error> {
                let mut values = Map::new();
                while let Some((key, Unique(v))) = map.next_entry::<String, Unique>()? {
                    if values.insert(key, v).is_some() {
                        return Err(de::Error::custom("duplicate key"));
                    }
                }
                Ok(Unique(Value::Object(values)))
            }
        }
        deserializer.deserialize_any(Strict)
    }
}
pub(crate) fn parse(raw: &[u8]) -> Result<Value, super::Error> {
    serde_json::from_slice::<Unique>(raw)
        .map(|v| v.0)
        .map_err(|_| super::Error::Failed)
}
pub(crate) fn pages(raw: &[u8]) -> Result<Value, super::Error> {
    let mut rows = Vec::new();
    for item in serde_json::Deserializer::from_slice(raw).into_iter::<Unique>() {
        match item {
            Ok(Unique(Value::Array(page))) => rows.extend(page),
            _ => return Err(super::Error::Failed),
        }
    }
    Ok(Value::Array(rows))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_duplicate_keys_at_every_depth_and_trailing_frames() {
        for raw in [
            r#"{"a":1,"a":2}"#,
            r#"{"unused":[{"x":0,"x":1}]}"#,
            "{} {}",
            "NaN",
            "1e9999",
        ] {
            assert!(parse(raw.as_bytes()).is_err(), "{raw}");
        }
        assert!(parse(br#"{"a":[true,null,1,-2,1.5,"text"]}"#).is_ok());
    }
    #[test]
    fn filtered_pagination_flattens_only_arrays() {
        assert_eq!(
            pages(b"[]\n[{\"body\":\"x\"}]\n[]").unwrap(),
            serde_json::json!([{"body":"x"}])
        );
        for raw in [b"[] {}".as_slice(), b"[{\"x\":1,\"x\":2}]", b"[", b"null"] {
            assert!(pages(raw).is_err());
        }
    }
}
