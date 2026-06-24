//! vrm-watch — VRM ファイル監視 + Blender レンダー + ledger 登録ツール
//!
//! # 使い方
//!
//! ```text
//! vrm-watch \
//!   --watch-dir <dir>                    # VRM 投下を監視するディレクトリ
//!   --output-dir <dir>                   # render.py の出力先
//!   --render-py <path>                   # render.py のパス
//!   [--blender <path>]                   # blender バイナリパス (デフォルト: blender)
//!   [--ledger <path>]                    # ledger DB パス (デフォルト: ~/.vrm-pipeline/ledger.db)
//!   [--ledger-bin <path>]                # ledger バイナリパス (デフォルト: ledger)
//!   [--golden-dir <dir>]                 # eval-A の golden ディレクトリ
//!   [--noise-floor-threshold <f32>]      # eval-A のアラート閾値 (デフォルト: 10.0)
//!   [--prompt <text>]                    # 台帳 INSERT 時の prompt (デフォルト: "")
//! ```
//!
//! # 動作フロー
//!
//! 1. `--watch-dir` を `notify` で監視（RecursiveMode::NonRecursive）
//! 2. `.vrm` 拡張子のファイルが Create/Modify されたら処理開始
//! 3. Blender をバックグラウンド実行して render.py を呼び出す
//! 4. render.py の stdout JSON を読んで Manifest を取得
//! 5. eval-A: manifest に `phash_signal` があり、閾値超えなら stderr に ALERT を出力
//! 6. `ledger insert` コマンドを実行して成果物を登録
//! 7. 正常完了を tracing::info で出力

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::mpsc::channel;

use anyhow::{Context, Result};
use clap::Parser;
use notify::{EventKind, RecursiveMode, Watcher};
use serde::{Deserialize, Serialize};
use tracing::{error, info, warn};

/// VRM ファイルを監視して Blender レンダー + ledger 登録を行うウォッチャー
#[derive(Parser, Debug)]
#[command(name = "vrm-watch", about = "VRM file watcher with blender+ledger pipeline")]
struct Args {
    /// VRM 投下を監視するディレクトリ
    #[arg(long)]
    watch_dir: PathBuf,

    /// render.py の出力先ディレクトリ
    #[arg(long)]
    output_dir: PathBuf,

    /// blender バイナリパス
    #[arg(long, default_value = "blender")]
    blender: String,

    /// render.py のパス
    #[arg(long)]
    render_py: PathBuf,

    /// ledger DB パス
    #[arg(long)]
    ledger: Option<PathBuf>,

    /// ledger バイナリパス
    #[arg(long, default_value = "ledger")]
    ledger_bin: String,

    /// eval-A の golden ディレクトリ
    #[arg(long)]
    golden_dir: Option<PathBuf>,

    /// eval-A のアラート閾値
    #[arg(long, default_value_t = 10.0)]
    noise_floor_threshold: f32,

    /// 台帳 INSERT 時の prompt
    #[arg(long, default_value = "")]
    prompt: String,
}

/// CLI 設定をまとめた構造体
#[derive(Debug, Clone)]
pub struct Config {
    pub watch_dir: PathBuf,
    pub output_dir: PathBuf,
    pub blender: String,
    pub render_py: PathBuf,
    pub ledger: PathBuf,
    pub ledger_bin: String,
    pub golden_dir: Option<PathBuf>,
    pub noise_floor_threshold: f32,
    pub prompt: String,
}

impl Config {
    fn from_args(args: Args) -> Self {
        let ledger = args.ledger.unwrap_or_else(|| {
            dirs_next().join(".vrm-pipeline").join("ledger.db")
        });
        Self {
            watch_dir: args.watch_dir,
            output_dir: args.output_dir,
            blender: args.blender,
            render_py: args.render_py,
            ledger,
            ledger_bin: args.ledger_bin,
            golden_dir: args.golden_dir,
            noise_floor_threshold: args.noise_floor_threshold,
            prompt: args.prompt,
        }
    }
}

fn dirs_next() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."))
}

/// render.py が stdout に出力する JSON マニフェスト
#[derive(Debug, Deserialize, Serialize, PartialEq)]
pub struct Manifest {
    /// レンダー出力ディレクトリ
    pub output_dir: String,
    /// 出力ファイルの一覧
    #[serde(default)]
    pub files: Vec<String>,
    /// eval-A 用 perceptual hash シグナル (dB 値など)
    pub phash_signal: Option<f32>,
    /// その他の追加フィールド
    #[serde(flatten)]
    pub extra: std::collections::HashMap<String, serde_json::Value>,
}

/// stdout の JSON テキストを Manifest にパースする
pub fn parse_manifest(stdout: &str) -> Result<Manifest> {
    // stdout の最後の JSON 行を探す（ログ行が混在することを想定）
    let manifest_line = stdout
        .lines()
        .filter(|l| l.trim_start().starts_with('{'))
        .last()
        .context("render.py の stdout に JSON オブジェクトが見つかりません")?;

    serde_json::from_str(manifest_line)
        .context("manifest JSON のパースに失敗しました")
}

/// eval-A: phash_signal が threshold を超えたら true（ALERT）
pub fn check_eval_a(manifest: &Manifest, threshold: f32) -> bool {
    match manifest.phash_signal {
        Some(signal) => signal > threshold,
        None => false,
    }
}

/// VRM ファイルを Blender でレンダーして Manifest を返す
pub fn run_blender(vrm: &Path, output: &Path, config: &Config) -> Result<Manifest> {
    let mut cmd = Command::new(&config.blender);
    cmd.arg("--background")
        .arg("--python")
        .arg(&config.render_py)
        .arg("--")
        .arg("--vrm")
        .arg(vrm)
        .arg("--output")
        .arg(output);

    if let Some(golden) = &config.golden_dir {
        cmd.arg("--golden").arg(golden);
    }

    info!(
        vrm = %vrm.display(),
        output = %output.display(),
        "blender 実行開始"
    );

    let result = cmd
        .output()
        .with_context(|| format!("blender の起動に失敗しました: {}", config.blender))?;

    let stdout = String::from_utf8_lossy(&result.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&result.stderr).into_owned();

    if !result.status.success() {
        anyhow::bail!(
            "blender が異常終了しました (exit: {})\nstderr: {}",
            result.status,
            stderr
        );
    }

    parse_manifest(&stdout)
}

/// ledger insert コマンドを実行する
pub fn run_ledger_insert(output_dir: &Path, config: &Config) -> Result<()> {
    let mut cmd = Command::new(&config.ledger_bin);
    cmd.arg("insert")
        .arg("--prompt")
        .arg(&config.prompt)
        .arg("--r0-dir")
        .arg(output_dir)
        .arg("--db")
        .arg(&config.ledger);

    info!(
        output_dir = %output_dir.display(),
        "ledger insert 実行"
    );

    let result = cmd
        .output()
        .with_context(|| format!("ledger の起動に失敗しました: {}", config.ledger_bin))?;

    if !result.status.success() {
        let stderr = String::from_utf8_lossy(&result.stderr);
        anyhow::bail!(
            "ledger insert が異常終了しました (exit: {})\nstderr: {}",
            result.status,
            stderr
        );
    }

    Ok(())
}

/// VRM ファイル検出時の処理
pub fn handle_vrm_event(path: &Path, config: &Config) -> Result<()> {
    // .vrm 拡張子のみ処理
    match path.extension().and_then(|e| e.to_str()) {
        Some("vrm") => {}
        _ => {
            info!(path = %path.display(), "非 .vrm ファイルをスキップ");
            return Ok(());
        }
    }

    let stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("unknown");

    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);

    let output_dir = config
        .output_dir
        .join(format!("{}_{}", stem, timestamp));

    std::fs::create_dir_all(&output_dir)
        .with_context(|| format!("出力ディレクトリの作成に失敗: {}", output_dir.display()))?;

    // Blender 実行
    let manifest = run_blender(path, &output_dir, config)?;

    // eval-A チェック
    if check_eval_a(&manifest, config.noise_floor_threshold) {
        let signal = manifest.phash_signal.unwrap_or(0.0);
        eprintln!(
            "ALERT: phash_signal={:.2} がノイズフロア閾値 {:.2} を超えました (vrm: {})",
            signal,
            config.noise_floor_threshold,
            path.display()
        );
        warn!(
            signal = signal,
            threshold = config.noise_floor_threshold,
            vrm = %path.display(),
            "eval-A ALERT: phash_signal が閾値超過"
        );
    }

    // ledger insert
    run_ledger_insert(&output_dir, config)?;

    info!(
        vrm = %path.display(),
        output_dir = %output_dir.display(),
        "VRM 処理が正常に完了しました"
    );

    Ok(())
}

/// ファイル監視ループ（イベントを受け取るたびに handle_vrm_event を呼ぶ）
pub fn watch_loop(config: &Config) -> Result<()> {
    let (tx, rx) = channel();

    let mut watcher =
        notify::recommended_watcher(move |res: notify::Result<notify::Event>| {
            if let Ok(event) = res {
                let _ = tx.send(event);
            }
        })
        .context("ファイルウォッチャーの初期化に失敗しました")?;

    watcher
        .watch(&config.watch_dir, RecursiveMode::NonRecursive)
        .with_context(|| {
            format!(
                "ディレクトリの監視を開始できませんでした: {}",
                config.watch_dir.display()
            )
        })?;

    info!(
        watch_dir = %config.watch_dir.display(),
        "VRM ファイルの監視を開始しました"
    );

    for event in rx {
        match event.kind {
            EventKind::Create(_) | EventKind::Modify(_) => {
                for path in event.paths {
                    if let Err(e) = handle_vrm_event(&path, config) {
                        error!(path = %path.display(), error = %e, "VRM イベント処理中にエラーが発生しました");
                    }
                }
            }
            _ => {}
        }
    }

    Ok(())
}

fn main() -> Result<()> {
    tracing_subscriber::fmt::init();

    let args = Args::parse();
    let config = Config::from_args(args);

    watch_loop(&config)
}

#[cfg(test)]
mod tests {
    use super::*;

    // parse_manifest: 有効な JSON を渡して Manifest がパースできること
    #[test]
    fn test_parse_manifest_valid() {
        let json = r#"{"output_dir": "/tmp/out", "files": ["a.png", "b.png"], "phash_signal": 5.5}"#;
        let manifest = parse_manifest(json).expect("パースに失敗しました");
        assert_eq!(manifest.output_dir, "/tmp/out");
        assert_eq!(manifest.files, vec!["a.png", "b.png"]);
        assert_eq!(manifest.phash_signal, Some(5.5));
    }

    // parse_manifest: ログ行が混在していても最後の JSON 行をパースできること
    #[test]
    fn test_parse_manifest_with_log_lines() {
        let stdout = "INFO loading blender\nDEBUG rendering frame 1\n{\"output_dir\": \"/tmp/x\", \"files\": []}";
        let manifest = parse_manifest(stdout).expect("ログ行混在のパースに失敗しました");
        assert_eq!(manifest.output_dir, "/tmp/x");
        assert!(manifest.files.is_empty());
        assert!(manifest.phash_signal.is_none());
    }

    // parse_manifest: JSON がなければエラーを返すこと
    #[test]
    fn test_parse_manifest_no_json_returns_error() {
        let stdout = "no json here at all";
        assert!(parse_manifest(stdout).is_err());
    }

    // check_eval_a: phash_signal が閾値以下なら false
    #[test]
    fn test_check_eval_a_below_threshold() {
        let manifest = Manifest {
            output_dir: "/tmp".to_string(),
            files: vec![],
            phash_signal: Some(5.0),
            extra: Default::default(),
        };
        assert!(!check_eval_a(&manifest, 10.0));
    }

    // check_eval_a: phash_signal が閾値ちょうどなら false（超えていない）
    #[test]
    fn test_check_eval_a_at_threshold() {
        let manifest = Manifest {
            output_dir: "/tmp".to_string(),
            files: vec![],
            phash_signal: Some(10.0),
            extra: Default::default(),
        };
        assert!(!check_eval_a(&manifest, 10.0));
    }

    // check_eval_a: phash_signal が閾値超えなら true
    #[test]
    fn test_check_eval_a_above_threshold() {
        let manifest = Manifest {
            output_dir: "/tmp".to_string(),
            files: vec![],
            phash_signal: Some(15.5),
            extra: Default::default(),
        };
        assert!(check_eval_a(&manifest, 10.0));
    }

    // check_eval_a: phash_signal が None なら false
    #[test]
    fn test_check_eval_a_no_signal() {
        let manifest = Manifest {
            output_dir: "/tmp".to_string(),
            files: vec![],
            phash_signal: None,
            extra: Default::default(),
        };
        assert!(!check_eval_a(&manifest, 10.0));
    }

    // handle_non_vrm_extension: .txt ファイルは無視されること
    #[test]
    fn test_handle_non_vrm_extension_skips() {
        // Config に実在しないパスを渡してもスキップロジックだけを確認する
        let config = Config {
            watch_dir: PathBuf::from("/tmp"),
            output_dir: PathBuf::from("/tmp/out"),
            blender: "blender".to_string(),
            render_py: PathBuf::from("/tmp/render.py"),
            ledger: PathBuf::from("/tmp/ledger.db"),
            ledger_bin: "ledger".to_string(),
            golden_dir: None,
            noise_floor_threshold: 10.0,
            prompt: "".to_string(),
        };

        // .txt ファイルを渡すと blender を起動せず Ok(()) を返すはず
        let result = handle_vrm_event(Path::new("/tmp/test.txt"), &config);
        assert!(result.is_ok(), "非 VRM ファイルは Ok(()) を返すべき: {:?}", result);
    }

    // handle_vrm_event: .vrm ファイルで blender 未インストール環境ではエラーを返すこと
    #[test]
    fn test_handle_vrm_event_blender_not_found_returns_error() {
        use tempfile::TempDir;
        let tmp = TempDir::new().unwrap();
        let vrm_path = tmp.path().join("model.vrm");
        std::fs::write(&vrm_path, b"dummy vrm content").unwrap();

        let config = Config {
            watch_dir: tmp.path().to_path_buf(),
            output_dir: tmp.path().to_path_buf(),
            blender: "nonexistent-blender-bin-xyz".to_string(),
            render_py: PathBuf::from("/tmp/render.py"),
            ledger: PathBuf::from("/tmp/ledger.db"),
            ledger_bin: "ledger".to_string(),
            golden_dir: None,
            noise_floor_threshold: 10.0,
            prompt: "".to_string(),
        };

        let result = handle_vrm_event(&vrm_path, &config);
        assert!(result.is_err(), "存在しない blender はエラーを返すべき");
    }
}
