//! Spec for `ledger check`: read-only, fail-closed, three-valued integrity gate.
//!
//! exit 0 = clean, 1 = violations (one stdout line each naming id + rule),
//! 2 = cannot determine (stderr message; never 0).
//!
//! asset_ref format (db.rs / schema.rs / README / capture-object.md): a JSON
//! object text mapping kind -> path, default '{}', e.g. {"glb":"/abs/x.glb"}.
//! The documented writers (capture-object.md:103, README:322) store ABSOLUTE
//! paths; the only relative value ("a.glb") is in a db.rs unit test and no
//! base directory is documented. So R3 tests use absolute paths only.

use rusqlite::Connection;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

fn ledger(db: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_ledger"))
        .arg("--db")
        .arg(db)
        .args(args)
        .output()
        .expect("spawn ledger")
}

fn code(o: &Output) -> i32 {
    o.status.code().expect("exit code (not killed by signal)")
}

fn stdout(o: &Output) -> String {
    String::from_utf8_lossy(&o.stdout).into_owned()
}

fn new_db() -> (tempfile::TempDir, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("ledger.db");
    let o = ledger(&db, &["init"]);
    assert_eq!(code(&o), 0, "init failed: {:?}", o);
    (dir, db)
}

fn insert(db: &Path, parent: Option<&str>, asset_ref: Option<&str>) -> String {
    let mut args = vec!["insert", "--prompt", "p"];
    if let Some(p) = parent {
        args.extend(["--parent-id", p]);
    }
    if let Some(a) = asset_ref {
        args.extend(["--asset-ref", a]);
    }
    let o = ledger(db, &args);
    assert_eq!(code(&o), 0, "insert failed: {:?}", o);
    stdout(&o).trim().to_string()
}

fn sql(db: &Path, stmt: &str) {
    Connection::open(db).unwrap().execute_batch(stmt).unwrap();
}

fn check(db: &Path) -> Output {
    ledger(db, &["check"])
}

/// clap itself exits 2 for an unknown subcommand, which would make the
/// exit-2 tests pass vacuously before `check` exists. Reject that cause.
fn assert_cannot_determine(o: &Output, ctx: &str) {
    assert_eq!(code(o), 2, "{ctx}: {:?}", o);
    let err = String::from_utf8_lossy(&o.stderr);
    assert!(!err.is_empty(), "{ctx}: message must be on stderr");
    assert!(
        !err.contains("unrecognized subcommand"),
        "{ctx}: exit 2 came from clap, not from check: {err}"
    );
}

#[test]
fn clean_db_exits_0() {
    let (_d, db) = new_db();
    let root = insert(&db, None, None);
    let child = insert(&db, Some(&root), None);
    insert(&db, Some(&child), None);
    let o = check(&db);
    assert_eq!(code(&o), 0, "{:?}", o);
}

#[test]
fn empty_db_exits_0() {
    let (_d, db) = new_db();
    assert_eq!(code(&check(&db)), 0);
}

#[test]
fn r1_parent_missing_exits_1_naming_id() {
    let (_d, db) = new_db();
    let orphan = insert(&db, Some("no-such-parent-id"), None);
    let o = check(&db);
    assert_eq!(code(&o), 1, "{:?}", o);
    let out = stdout(&o);
    assert!(
        out.lines().any(|l| l.contains(&orphan)),
        "stdout must name the record id {orphan}: {out}"
    );
    assert!(out.contains("parent-missing"), "rule name missing: {out}");
}

#[test]
fn r1_empty_string_parent_is_not_a_violation() {
    let (_d, db) = new_db();
    let id = insert(&db, None, None);
    sql(
        &db,
        &format!("UPDATE records SET parent_id = '' WHERE id = '{id}'"),
    );
    assert_eq!(code(&check(&db)), 0);
}

#[test]
fn r2_self_parent_exits_1() {
    let (_d, db) = new_db();
    let id = insert(&db, None, None);
    sql(
        &db,
        &format!("UPDATE records SET parent_id = id WHERE id = '{id}'"),
    );
    let o = check(&db);
    assert_eq!(code(&o), 1, "{:?}", o);
    let out = stdout(&o);
    assert!(
        out.lines()
            .any(|l| l.contains(&id) && l.contains("lineage-cycle")),
        "{out}"
    );
}

#[test]
fn r2_two_cycle_exits_1_naming_both() {
    let (_d, db) = new_db();
    let a = insert(&db, None, None);
    let b = insert(&db, Some(&a), None);
    sql(
        &db,
        &format!("UPDATE records SET parent_id = '{b}' WHERE id = '{a}'"),
    );
    let o = check(&db);
    assert_eq!(code(&o), 1, "{:?}", o);
    let out = stdout(&o);
    for id in [&a, &b] {
        assert!(
            out.lines()
                .any(|l| l.contains(id.as_str()) && l.contains("lineage-cycle")),
            "cycle member {id} not reported: {out}"
        );
    }
}

#[test]
fn r3_asset_missing_exits_1_naming_id() {
    let (d, db) = new_db();
    let missing = d.path().join("gone.glb");
    let ar = format!("{{\"glb\":\"{}\"}}", missing.display());
    let id = insert(&db, None, Some(&ar));
    let o = check(&db);
    assert_eq!(code(&o), 1, "{:?}", o);
    let out = stdout(&o);
    assert!(
        out.lines()
            .any(|l| l.contains(&id) && l.contains("asset-missing")),
        "{out}"
    );
}

#[test]
fn r3_existing_asset_exits_0() {
    let (d, db) = new_db();
    let f = d.path().join("here.glb");
    std::fs::write(&f, b"x").unwrap();
    let ar = format!("{{\"glb\":\"{}\"}}", f.display());
    insert(&db, None, Some(&ar));
    let o = check(&db);
    assert_eq!(code(&o), 0, "{:?}", o);
}

#[test]
fn r3_default_empty_object_asset_ref_is_clean() {
    let (_d, db) = new_db();
    insert(&db, None, Some("{}"));
    assert_eq!(code(&check(&db)), 0);
}

#[test]
fn missing_db_file_exits_2_and_does_not_create_it() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("nope.db");
    let o = check(&db);
    assert_cannot_determine(&o, "missing db");
    assert!(!db.exists(), "check must not create the DB");
}

#[test]
fn garbage_file_exits_2() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("garbage.db");
    std::fs::write(
        &db,
        b"this is definitely not a sqlite database, just text padding.....",
    )
    .unwrap();
    let before = std::fs::read(&db).unwrap();
    let o = check(&db);
    assert_cannot_determine(&o, "garbage");
    assert_eq!(std::fs::read(&db).unwrap(), before, "garbage file modified");
}

#[test]
fn missing_required_column_exits_2() {
    for missing in ["id", "parent_id", "asset_ref"] {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("l.db");
        let decl: Vec<String> = ["id", "parent_id", "asset_ref"]
            .into_iter()
            .filter(|c| *c != missing)
            .map(|c| format!("{c} TEXT"))
            .collect();
        sql(&db, &format!("CREATE TABLE records ({});", decl.join(", ")));
        let o = check(&db);
        assert_cannot_determine(&o, &format!("missing column {missing}"));
    }
}

#[test]
fn no_records_table_exits_2() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("l.db");
    sql(&db, "CREATE TABLE other (x TEXT);");
    assert_cannot_determine(&check(&db), "no records table");
}

fn snapshot(db: &Path) -> (Vec<u8>, std::time::SystemTime) {
    (
        std::fs::read(db).unwrap(),
        std::fs::metadata(db).unwrap().modified().unwrap(),
    )
}

#[test]
fn r4_check_does_not_modify_current_schema_db() {
    let (_d, db) = new_db();
    let a = insert(&db, None, None);
    insert(&db, Some(&a), None);
    let before = snapshot(&db);
    std::thread::sleep(std::time::Duration::from_millis(50));
    assert_eq!(code(&check(&db)), 0);
    assert_eq!(snapshot(&db), before, "check modified a clean DB");
}

#[test]
fn r4_check_does_not_migrate_older_schema_db() {
    // Older schema: has the 3 required columns but lacks derived/image_ref/status,
    // which open() would ALTER in. check must leave the file untouched.
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("old.db");
    sql(
        &db,
        "CREATE TABLE records (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, \
         prompt TEXT NOT NULL DEFAULT '', asset_ref TEXT NOT NULL DEFAULT '{}', parent_id TEXT);\
         INSERT INTO records (id, timestamp, asset_ref, parent_id) VALUES ('a','t','{}',NULL);\
         INSERT INTO records (id, timestamp, asset_ref, parent_id) VALUES ('b','t','{}','a');",
    );
    let before = snapshot(&db);
    std::thread::sleep(std::time::Duration::from_millis(50));
    let o = check(&db);
    assert_eq!(code(&o), 0, "{:?}", o);
    assert_eq!(
        snapshot(&db),
        before,
        "check migrated/modified an older-schema DB"
    );
    let cols: Vec<String> = {
        let c = Connection::open(&db).unwrap();
        let mut s = c.prepare("PRAGMA table_info(records)").unwrap();
        s.query_map([], |r| r.get::<_, String>(1))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    };
    assert!(
        !cols.contains(&"derived".to_string()),
        "schema was migrated: {cols:?}"
    );
}

#[test]
fn r4_check_does_not_modify_db_with_violations() {
    let (_d, db) = new_db();
    insert(&db, Some("ghost"), None);
    let before = snapshot(&db);
    std::thread::sleep(std::time::Duration::from_millis(50));
    assert_eq!(code(&check(&db)), 1);
    assert_eq!(snapshot(&db), before);
}

// ---- asset_ref undetermined cases (coordinator rulings) -------------------
// Each must exit 2 with a stderr message naming the record id (never 0/1-only).

fn set_asset_ref(db: &Path, id: &str, value: &str) {
    Connection::open(db)
        .unwrap()
        .execute(
            "UPDATE records SET asset_ref = ?1 WHERE id = ?2",
            rusqlite::params![value, id],
        )
        .unwrap();
}

fn assert_undetermined_names(db: &Path, id: &str, ctx: &str) {
    let o = check(db);
    assert_cannot_determine(&o, ctx);
    let err = String::from_utf8_lossy(&o.stderr);
    assert!(
        err.contains(id),
        "{ctx}: stderr must name record {id}: {err}"
    );
}

fn undetermined_case(value: &str, ctx: &str) {
    let (_d, db) = new_db();
    let id = insert(&db, None, None);
    set_asset_ref(&db, &id, value);
    assert_undetermined_names(&db, &id, ctx);
}

#[test]
fn r3_relative_path_is_undetermined_exit_2() {
    undetermined_case(r#"{"glb":"a.glb"}"#, "relative path");
    undetermined_case(r#"{"glb":"./out/a.glb"}"#, "dot-relative path");
}

#[test]
fn r3_invalid_json_is_undetermined_exit_2() {
    undetermined_case("not json at all", "invalid json");
    undetermined_case("{\"glb\":", "truncated json");
}

#[test]
fn r3_non_object_json_is_undetermined_exit_2() {
    undetermined_case(r#"["/abs/a.glb"]"#, "json array");
    undetermined_case(r#""/abs/a.glb""#, "json string");
    undetermined_case("42", "json number");
    undetermined_case("null", "json null");
}

#[test]
fn r3_non_string_value_is_undetermined_exit_2() {
    undetermined_case(r#"{"glb":42}"#, "number value");
    undetermined_case(r#"{"glb":null}"#, "null value");
    undetermined_case(r#"{"glb":["/abs/a.glb"]}"#, "array value");
    undetermined_case(r#"{"glb":{"p":"/abs/a.glb"}}"#, "object value");
}

#[test]
fn r3_empty_string_value_is_undetermined_exit_2() {
    undetermined_case(r#"{"glb":""}"#, "empty string value");
}

#[test]
fn r3_one_bad_value_among_good_ones_is_undetermined() {
    let (d, db) = new_db();
    let f = d.path().join("ok.glb");
    std::fs::write(&f, b"x").unwrap();
    let id = insert(&db, None, None);
    set_asset_ref(
        &db,
        &id,
        &format!(r#"{{"glb":"{}","vrm":"rel.vrm"}}"#, f.display()),
    );
    assert_undetermined_names(&db, &id, "good + relative");
}

#[test]
fn undetermined_wins_over_violations_but_violations_still_printed() {
    let (_d, db) = new_db();
    let orphan = insert(&db, Some("ghost-parent"), None);
    let bad = insert(&db, None, None);
    set_asset_ref(&db, &bad, r#"{"glb":"relative.glb"}"#);
    let o = check(&db);
    assert_cannot_determine(&o, "precedence");
    let err = String::from_utf8_lossy(&o.stderr);
    assert!(err.contains(&bad), "stderr must name {bad}: {err}");
    let out = stdout(&o);
    assert!(
        out.lines()
            .any(|l| l.contains(&orphan) && l.contains("parent-missing")),
        "violation must still be printed on stdout: {out}"
    );
}

#[test]
fn undetermined_check_does_not_modify_db() {
    let (_d, db) = new_db();
    let id = insert(&db, None, None);
    set_asset_ref(&db, &id, "garbage");
    let before = snapshot(&db);
    std::thread::sleep(std::time::Duration::from_millis(50));
    assert_undetermined_names(&db, &id, "undetermined readonly");
    assert_eq!(snapshot(&db), before);
}
