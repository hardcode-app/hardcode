//! The HardCode desktop app: a window over the `hardcode` command line.
//!
//! Rust does three things here. It finds the CLI, runs it with `--json` for
//! the page, and streams the output of long commands (start, rebuild,
//! delete) to the page's console. All the logic lives in the CLI, which
//! keeps the profiles in the user's HardCode home (`--config DIR` on the
//! app's command line passes another one on); the page (`src/app.js`)
//! shows what it says. A release also asks the project's GitHub releases
//! for a newer app, and installs it when the page says so.

use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{LazyLock, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::utils::config::PluginConfig;
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_updater::{Update, UpdaterExt};

/// Whether the app was started with `--smoke`: render every view, report, exit.
static SMOKE: AtomicBool = AtomicBool::new(false);

/// The folder `--config` named on the app's command line, handed to every
/// CLI run as `HARDCODE_CONFIG`; without one, the CLI uses its own default
/// (or a `HARDCODE_CONFIG` the app was started with).
static CONFIG: OnceLock<Option<String>> = OnceLock::new();

/// The streaming commands that are running, by id, so the page can stop one.
static STREAMS: LazyLock<Mutex<HashMap<String, Child>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// No `hardcode` call the window waits on should take longer than this.
const RUN_TIMEOUT: Duration = Duration::from_secs(15 * 60);

/// How long to wait for a command's output pipes to drain once it has ended.
/// After a kill, something it started may still hold them.
const DRAIN_TIMEOUT: Duration = Duration::from_secs(2);

/// What `hardcode` printed and how it exited.
#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct CliResult {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

/// One line of a streaming command's output, for the page's console.
#[derive(Serialize, Clone)]
struct OutputLine {
    id: String,
    line: String,
}

/// A streaming command has ended, with this exit code (-1: killed or lost).
#[derive(Serialize, Clone)]
struct Done {
    id: String,
    code: i32,
}

/// How to start the CLI: a program, and the arguments that come before the
/// CLI's own (the script, when the program is Python).
#[derive(Debug, Clone, PartialEq)]
pub struct CliCommand {
    pub program: PathBuf,
    pub prefix: Vec<String>,
}

// ---------------------------------------------------------------------------
// Finding the CLI
// ---------------------------------------------------------------------------

/// `--config DIR` or `--config=DIR` among the app's arguments, if any.
pub fn config_argument(args: &[String]) -> Option<String> {
    let mut arguments = args.iter();

    while let Some(argument) = arguments.next() {
        if argument == "--config" {
            return arguments.next().cloned();
        }
        if let Some(value) = argument.strip_prefix("--config=") {
            return Some(value.to_string());
        }
    }

    None
}

/// The checkout this app was built from: a development build runs its
/// `cli/hardcode` when nothing else is there.
fn source_checkout() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("..")
}

/// How to start the CLI, in order of preference:
///
/// 1. `$HARDCODE_CLI`: a program, optionally followed by arguments;
/// 2. the sidecar bundled next to this executable in a release;
/// 3. `python3 cli/hardcode` in `checkout`, the source a development build
///    was made from.
pub fn cli_command(
    exe_dir: Option<&Path>,
    env_override: Option<&str>,
    checkout: &Path,
) -> Result<CliCommand, String> {
    if let Some(spec) = env_override.map(str::trim).filter(|spec| !spec.is_empty()) {
        let mut parts = spec.split_whitespace().map(String::from);
        let program = PathBuf::from(parts.next().unwrap_or_default());

        return Ok(CliCommand {
            program,
            prefix: parts.collect(),
        });
    }

    if let Some(dir) = exe_dir {
        let sidecar = dir.join(if cfg!(windows) {
            "hardcode.exe"
        } else {
            "hardcode"
        });

        if sidecar.is_file() {
            return Ok(CliCommand {
                program: sidecar,
                prefix: vec![],
            });
        }
    }

    let script = checkout.join("cli").join("hardcode");
    if script.is_file() {
        let python = if cfg!(windows) { "python" } else { "python3" };

        return Ok(CliCommand {
            program: PathBuf::from(python),
            prefix: vec![script.to_string_lossy().into_owned()],
        });
    }

    Err(
        "the hardcode command was not found: no bundled copy beside the app (set HARDCODE_CLI to one)"
            .into(),
    )
}

/// The directory this executable is in, where a release keeps the sidecar.
fn exe_dir() -> Option<PathBuf> {
    std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf))
}

/// The command line for `hardcode <args>`, with the home `--config` named.
fn build(args: &[String]) -> Result<Command, String> {
    let env_override = std::env::var("HARDCODE_CLI").ok();
    let spec = cli_command(
        exe_dir().as_deref(),
        env_override.as_deref(),
        &source_checkout(),
    )?;

    let mut command = Command::new(&spec.program);
    command.args(&spec.prefix).args(args);

    if let Some(Some(config)) = CONFIG.get() {
        command.env("HARDCODE_CONFIG", config);
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;

        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    Ok(command)
}

// ---------------------------------------------------------------------------
// Running commands
// ---------------------------------------------------------------------------

/// Start a command as the leader of its own process group, so that
/// `docker compose` and whatever else it starts go with it when it is
/// stopped (see `kill_tree`). Every CLI run goes through here.
///
/// Its input is empty, never the terminal the app was started from: a
/// process outside the terminal's foreground group that reads from it is
/// stopped by the kernel (SIGTTIN), and `docker compose exec` reads its
/// input. That stopped the whole group, the CLI included, so reading VS
/// Code's token hung forever when the app was run from a shell.
fn spawn_isolated(command: &mut Command) -> std::io::Result<Child> {
    command.stdin(Stdio::null());

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;

        command.process_group(0);
    }

    command.spawn()
}

/// End a command and everything it started.
///
/// The CLI's work is done by `docker compose`, which holds the output pipes:
/// killing the CLI alone would leave the build running and the console
/// waiting for it.
pub fn kill_tree(child: &mut Child) {
    #[cfg(unix)]
    {
        // A negative pid addresses the whole process group.
        let group = format!("-{}", child.id());

        let _ = Command::new("kill").args(["-TERM", "--", &group]).status();
        thread::sleep(Duration::from_millis(500)); // a moment for Compose to clean up
        let _ = Command::new("kill").args(["-KILL", "--", &group]).status();
    }

    #[cfg(windows)]
    {
        let pid = child.id().to_string();
        let _ = Command::new("taskkill")
            .args(["/T", "/F", "/PID", &pid])
            .status();
    }

    let _ = child.kill();
    let _ = child.wait();
}

/// Read a pipe to its end in a thread of its own, so that neither pipe can
/// fill up and stall the command while the other is being read.
fn read_in_background<R: Read + Send + 'static>(pipe: Option<R>) -> thread::JoinHandle<Vec<u8>> {
    thread::spawn(move || {
        let mut bytes = Vec::new();
        if let Some(mut pipe) = pipe {
            let _ = pipe.read_to_end(&mut bytes);
        }
        bytes
    })
}

/// Wait for a reader thread to finish, but not past `deadline`: after a kill
/// something may still hold the pipe.
fn collect_within(handle: thread::JoinHandle<Vec<u8>>, deadline: Instant) -> Vec<u8> {
    while !handle.is_finished() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(20));
    }

    if handle.is_finished() {
        handle.join().unwrap_or_default()
    } else {
        Vec::new()
    }
}

/// Run a command to completion, or kill it once `timeout` has passed.
pub fn run_command(mut command: Command, timeout: Duration) -> Result<CliResult, String> {
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = spawn_isolated(&mut command)
        .map_err(|error| format!("could not start the hardcode command: {error}"))?;

    let stdout_reader = read_in_background(child.stdout.take());
    let stderr_reader = read_in_background(child.stderr.take());

    let started = Instant::now();
    let mut timed_out = false;

    let code = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status.code().unwrap_or(-1),

            Ok(None) if started.elapsed() > timeout => {
                kill_tree(&mut child);
                timed_out = true;
                break -1;
            }

            Ok(None) => thread::sleep(Duration::from_millis(50)),
            Err(_) => break -1,
        }
    };

    let deadline = Instant::now() + DRAIN_TIMEOUT;
    let stdout = String::from_utf8_lossy(&collect_within(stdout_reader, deadline)).into_owned();
    let mut stderr = String::from_utf8_lossy(&collect_within(stderr_reader, deadline)).into_owned();

    if timed_out {
        stderr.push_str(&format!(
            "\nhardcode timed out after {} s",
            timeout.as_secs()
        ));
    }

    Ok(CliResult {
        code,
        stdout,
        stderr,
    })
}

/// Run the CLI to completion.
pub fn run_cli(args: &[String]) -> Result<CliResult, String> {
    run_command(build(args)?, RUN_TIMEOUT)
}

// ---------------------------------------------------------------------------
// The commands the page calls
// ---------------------------------------------------------------------------

/// `run_cli` off the main thread, so a slow `docker compose` never freezes the window.
#[tauri::command]
async fn cli_run(args: Vec<String>) -> Result<CliResult, String> {
    let task = tauri::async_runtime::spawn_blocking(move || run_cli(&args));

    match task.await {
        Ok(result) => result,
        Err(error) => Err(error.to_string()),
    }
}

/// Send each line a pipe produces to the page, as `cli-output` events.
fn forward_lines<R: Read + Send + 'static>(
    pipe: R,
    app: AppHandle,
    id: String,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        for line in BufReader::new(pipe).lines().map_while(Result::ok) {
            let event = OutputLine {
                id: id.clone(),
                line,
            };
            let _ = app.emit("cli-output", event);
        }
    })
}

/// Run the CLI, sending each output line to the page as it appears
/// (`cli-output` events), then `cli-done` with the exit code.
#[tauri::command]
fn cli_stream(app: AppHandle, id: String, args: Vec<String>) -> Result<(), String> {
    let mut command = build(&args)?;
    command.stdout(Stdio::piped()).stderr(Stdio::piped());

    let mut child = spawn_isolated(&mut command)
        .map_err(|error| format!("could not start the hardcode command: {error}"))?;
    let stdout = child.stdout.take().ok_or("no stdout")?;
    let stderr = child.stderr.take().ok_or("no stderr")?;

    let stdout_forwarder = forward_lines(stdout, app.clone(), id.clone());
    let stderr_forwarder = forward_lines(stderr, app.clone(), id.clone());

    // The child is kept in the table, for `cli_cancel`, until it has exited.
    STREAMS.lock().unwrap().insert(id.clone(), child);

    thread::spawn(move || {
        let code = loop {
            // The lock is held only for the poll, so `cli_cancel` can get in.
            let polled = STREAMS
                .lock()
                .unwrap()
                .get_mut(&id)
                .map(|child| child.try_wait());

            match polled {
                None => break -1,
                Some(Ok(Some(status))) => break status.code().unwrap_or(-1),
                Some(Ok(None)) => thread::sleep(Duration::from_millis(100)),
                Some(Err(_)) => break -1,
            }
        };

        // Let the forwarders pass on what the process left in the pipes; after
        // a stop, something it started may still hold them, so not forever.
        let deadline = Instant::now() + DRAIN_TIMEOUT;
        while (!stdout_forwarder.is_finished() || !stderr_forwarder.is_finished())
            && Instant::now() < deadline
        {
            thread::sleep(Duration::from_millis(50));
        }

        STREAMS.lock().unwrap().remove(&id);
        let _ = app.emit("cli-done", Done { id, code });
    });

    Ok(())
}

/// Stop a streaming command the page started, and everything it started.
#[tauri::command]
fn cli_cancel(id: String) -> Result<(), String> {
    match STREAMS.lock().unwrap().get_mut(&id) {
        Some(child) => {
            kill_tree(child);
            Ok(())
        }
        None => Err(format!("no running command {id}")),
    }
}

// ---------------------------------------------------------------------------
// Settings: the profile last used
// ---------------------------------------------------------------------------

fn settings_path(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .app_config_dir()
        .map_err(|error| error.to_string())?;

    Ok(dir.join("settings.json"))
}

/// Read settings from `path`; empty when there are none yet, or they can't be read.
pub fn load_settings(path: &Path) -> HashMap<String, serde_json::Value> {
    fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

/// Write settings to `path`, creating its directory.
pub fn save_settings(
    path: &Path,
    settings: &HashMap<String, serde_json::Value>,
) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).map_err(|error| error.to_string())?;
    }

    let text = serde_json::to_string_pretty(settings).map_err(|error| error.to_string())?;
    fs::write(path, text).map_err(|error| error.to_string())
}

#[tauri::command]
fn settings_load(app: AppHandle) -> Result<HashMap<String, serde_json::Value>, String> {
    Ok(load_settings(&settings_path(&app)?))
}

#[tauri::command]
fn settings_save(
    app: AppHandle,
    settings: HashMap<String, serde_json::Value>,
) -> Result<(), String> {
    save_settings(&settings_path(&app)?, &settings)
}

// ---------------------------------------------------------------------------
// Updates
// ---------------------------------------------------------------------------

/// The updater, in a release: the release workflow builds the app with
/// `plugins.updater` configured (the project's newest GitHub release, and
/// the key its installers are signed with). A development build has none,
/// and finds nothing to install.
struct Updates {
    configured: bool,
    /// The newer release the last check found, for `update_install`.
    found: Mutex<Option<Update>>,
}

/// A newer release, for the page.
#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct UpdateInfo {
    pub version: String,
    pub current: String,
    /// What latest.json says about it: the release's page.
    pub notes: String,
}

/// Whether the app's configuration sets the updater up.
pub fn updates_configured(plugins: &PluginConfig) -> bool {
    plugins.0.contains_key("updater")
}

/// Ask the project's releases whether there is a newer app: that release,
/// or none (always none in a build without an updater).
#[tauri::command]
async fn update_check(
    app: AppHandle,
    updates: State<'_, Updates>,
) -> Result<Option<UpdateInfo>, String> {
    if !updates.configured {
        return Ok(None);
    }

    let updater = app.updater().map_err(|error| error.to_string())?;
    let update = updater.check().await.map_err(|error| error.to_string())?;

    let info = update.as_ref().map(|update| UpdateInfo {
        version: update.version.clone(),
        current: update.current_version.clone(),
        notes: update.body.clone().unwrap_or_default(),
    });
    *updates.found.lock().unwrap() = update;

    Ok(info)
}

/// Download the release the last check found, check its signature against
/// the key this build trusts, install it, and start the new app. The
/// containers keep running meanwhile: they don't belong to the app.
#[tauri::command]
async fn update_install(app: AppHandle, updates: State<'_, Updates>) -> Result<(), String> {
    let update = updates
        .found
        .lock()
        .unwrap()
        .clone()
        .ok_or("there is no update to install")?;

    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|error| error.to_string())?;

    app.restart()
}

// ---------------------------------------------------------------------------
// Smoke mode, and start-up
// ---------------------------------------------------------------------------

#[tauri::command]
fn smoke_mode() -> bool {
    SMOKE.load(Ordering::Relaxed)
}

/// The page calls this once it has rendered every view in `--smoke` mode,
/// with every page error and unhandled rejection it saw, the profile it
/// showed, and the version of the newer release it was offered, if any.
#[tauri::command]
fn smoke_done(
    app: AppHandle,
    views: usize,
    errors: Vec<String>,
    profile: Option<String>,
    update: Option<String>,
) {
    let shown = profile
        .map(|name| format!(" profile={name}"))
        .unwrap_or_default();
    let offered = update
        .map(|version| format!(" update={version}"))
        .unwrap_or_default();

    if errors.is_empty() {
        println!("SMOKE OK: views={views}{shown}{offered}");
        app.exit(0);
        return;
    }

    eprintln!("SMOKE FAILED: {} page errors", errors.len());
    for error in &errors {
        eprintln!("  {error}");
    }
    app.exit(1);
}

/// Open the window, with the page's commands registered.
pub fn run() {
    let arguments: Vec<String> = std::env::args().collect();
    SMOKE.store(
        arguments.iter().any(|argument| argument == "--smoke"),
        Ordering::Relaxed,
    );
    let _ = CONFIG.set(config_argument(&arguments));

    let context = tauri::generate_context!();
    let configured = updates_configured(&context.config().plugins);

    let mut builder = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init());

    // The updater's plugin can't start without its configuration.
    if configured {
        builder = builder.plugin(tauri_plugin_updater::Builder::new().build());
    }

    builder
        .manage(Updates {
            configured,
            found: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            cli_run,
            cli_stream,
            cli_cancel,
            settings_load,
            settings_save,
            update_check,
            update_install,
            smoke_mode,
            smoke_done
        ])
        .setup(|app| {
            // A smoke run that hangs must still end, and fail.
            if SMOKE.load(Ordering::Relaxed) {
                let handle = app.handle().clone();
                thread::spawn(move || {
                    thread::sleep(Duration::from_secs(45));
                    eprintln!("SMOKE FAILED: the page did not report in time");
                    handle.exit(1);
                });
            }
            Ok(())
        })
        .run(context)
        .expect("error while running HardCode");
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    /// A fresh, empty temporary directory for one test.
    fn temp(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("hardcode-app-test-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn config_comes_from_the_command_line() {
        let args = |list: &[&str]| list.iter().map(|item| item.to_string()).collect::<Vec<_>>();

        assert_eq!(config_argument(&args(&["app"])), None);
        assert_eq!(
            config_argument(&args(&["app", "--config", "/Apps/hardcode"])),
            Some("/Apps/hardcode".into())
        );
        assert_eq!(
            config_argument(&args(&["app", "--smoke", "--config=/x"])),
            Some("/x".into())
        );
        assert_eq!(config_argument(&args(&["app", "--config"])), None);
    }

    #[test]
    fn cli_resolution_order() {
        let root = temp("cli");

        // Nothing available.
        assert!(cli_command(None, None, &root).is_err());

        // The checkout's script, through Python.
        fs::create_dir_all(root.join("cli")).unwrap();
        fs::write(
            root.join("cli").join("hardcode"),
            "#!/usr/bin/env python3\n",
        )
        .unwrap();

        let script = cli_command(None, None, &root).unwrap();
        assert!(script.program.to_string_lossy().starts_with("python"));
        assert_eq!(
            script.prefix,
            vec![root
                .join("cli")
                .join("hardcode")
                .to_string_lossy()
                .into_owned()]
        );

        // A bundled sidecar wins over the script.
        let exe_dir = root.join("bin");
        fs::create_dir_all(&exe_dir).unwrap();
        let sidecar = exe_dir.join(if cfg!(windows) {
            "hardcode.exe"
        } else {
            "hardcode"
        });
        fs::write(&sidecar, "").unwrap();

        assert_eq!(
            cli_command(Some(&exe_dir), None, &root).unwrap(),
            CliCommand {
                program: sidecar,
                prefix: vec![]
            }
        );

        // $HARDCODE_CLI wins over everything, and may carry arguments.
        let forced = cli_command(
            Some(&exe_dir),
            Some("  /opt/py/bin/python3 /somewhere/hardcode "),
            &root,
        )
        .unwrap();
        assert_eq!(
            forced,
            CliCommand {
                program: PathBuf::from("/opt/py/bin/python3"),
                prefix: vec!["/somewhere/hardcode".into()]
            }
        );

        let blank = cli_command(Some(&exe_dir), Some("   "), &root).unwrap();
        let default = cli_command(Some(&exe_dir), None, &root).unwrap();
        assert_eq!(blank.program, default.program, "blank override is ignored");

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn run_cli_reports_exit_code_and_output() {
        // The checkout this test is built from: its cli/hardcode. Both commands
        // exit while their arguments are parsed, so nothing is written.
        let result = run_cli(&["--version".into()]).expect("python3 cli/hardcode is runnable");
        assert_eq!(result.code, 0, "{}", result.stderr);
        assert!(result.stdout.starts_with("hardcode "), "{}", result.stdout);

        let failed = run_cli(&["no-such-command".into()]).unwrap();
        assert_ne!(failed.code, 0);
        assert!(
            failed.stderr.contains("invalid choice"),
            "{}",
            failed.stderr
        );
    }

    #[cfg(unix)]
    #[test]
    fn run_command_kills_what_the_command_started() {
        // sh starts sleep as a child of its own and waits for it; the pid file
        // lets the test look for that grandchild afterwards.
        let dir = temp("tree");
        let pidfile = dir.join("sleep.pid");

        let mut tree = Command::new("sh");
        tree.args([
            "-c",
            &format!("sleep 30 & echo $! > {}; wait", pidfile.display()),
        ]);

        let started = Instant::now();
        let result = run_command(tree, Duration::from_millis(300)).unwrap();
        assert_eq!(result.code, -1);
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "done without waiting for the grandchild's pipe"
        );

        let pid = fs::read_to_string(&pidfile).unwrap().trim().to_string();
        thread::sleep(Duration::from_millis(300));
        let alive = Command::new("kill")
            .args(["-0", &pid])
            .status()
            .map(|status| status.success())
            .unwrap_or(false);
        assert!(!alive, "the sleep the command started was stopped with it");

        fs::remove_dir_all(dir).unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn commands_never_get_the_terminal_as_input() {
        // What the command's input is: /dev/null, whatever the app's own is.
        let mut command = Command::new("readlink");
        command.arg("/proc/self/fd/0");

        let result = run_command(command, Duration::from_secs(5)).unwrap();
        assert_eq!(result.code, 0, "{}", result.stderr);
        assert_eq!(result.stdout.trim(), "/dev/null");
    }

    #[cfg(unix)]
    #[test]
    fn run_command_kills_after_the_timeout() {
        let mut slow = Command::new("sleep");
        slow.arg("5");

        let started = Instant::now();
        let result = run_command(slow, Duration::from_millis(300)).unwrap();
        assert_eq!(result.code, -1);
        assert!(result.stderr.contains("timed out"), "{}", result.stderr);
        assert!(
            started.elapsed() < Duration::from_secs(4),
            "the command was killed, not waited for"
        );

        let mut quick = Command::new("sh");
        quick.args(["-c", "echo out; echo err >&2; exit 3"]);

        let result = run_command(quick, Duration::from_secs(5)).unwrap();
        assert_eq!(
            (result.code, result.stdout.trim(), result.stderr.trim()),
            (3, "out", "err")
        );
    }

    #[test]
    fn the_updater_is_there_only_when_configured() {
        let mut plugins = PluginConfig::default();
        assert!(!updates_configured(&plugins));

        plugins
            .0
            .insert("updater".into(), serde_json::json!({ "pubkey": "key" }));
        assert!(updates_configured(&plugins));
    }

    #[test]
    fn no_configuration_in_the_checkout_has_the_updater() {
        // Only a release's build adds it (release/release.py
        // updater-config): a development or local build never replaces
        // itself with a downloaded one.
        let directory = Path::new(env!("CARGO_MANIFEST_DIR"));

        for name in ["tauri.conf.json", "tauri.release.conf.json"] {
            let text = fs::read_to_string(directory.join(name)).unwrap();
            let config: serde_json::Value = serde_json::from_str(&text).unwrap();

            assert!(
                config.pointer("/plugins/updater").is_none(),
                "{name} sets the updater up"
            );
            assert!(
                config.pointer("/bundle/createUpdaterArtifacts").is_none(),
                "{name} makes update files"
            );
        }
    }

    #[test]
    fn settings_round_trip() {
        let dir = temp("settings");
        let path = dir.join("nested").join("settings.json");
        assert!(load_settings(&path).is_empty());

        let mut settings = HashMap::new();
        settings.insert("profile".to_string(), serde_json::json!("old-node"));

        save_settings(&path, &settings).unwrap();
        assert_eq!(load_settings(&path), settings);

        fs::write(&path, "not json").unwrap();
        assert!(
            load_settings(&path).is_empty(),
            "corrupt settings read as empty"
        );

        fs::remove_dir_all(dir).unwrap();
    }
}
