"""A guided post-generation menu, with no OpenAI dependency."""
from __future__ import annotations

import getpass
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

from .console import ask, ask_yes_no, choose, heading
from .execution import kill
from .models import ProjectConfig
from .solve_native import ROOT, archive_package, copy_package, doctor, environment, native_action, runtime_python
from .solve4 import write_package_metadata
from .storage import StateStore, write_json, ensure_new_project
from .paths import project_code, language_code, checked_tree, checked_path
from .registry import load_manifest, safe_file


DEFAULT_SOLVE_HOST = "https://solve.edu.pl"


def download_for_editing(codename: str) -> StateStore:
    """Download a server package and register it as a resumable packer project."""

    project_code(codename)
    store = StateStore(codename)
    ensure_new_project(store)
    store.acquire_lock()
    ensure_new_project(store)
    from .input_sources import (
        IMAGE_SUFFIXES,
        _copy_tree_without_symlinks,
        package_drop_dir,
        package_has_tests,
        prepare_drop_zones,
    )
    destination = package_drop_dir(codename)
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError(
            f"Directory {destination} is not empty. The packer will not overwrite existing materials."
        )
    if not doctor()["ready"]:
        raise RuntimeError("Run ./run.sh --doctor to prepare Solve CLI.")

    connection = read_connection()
    print(f"Downloading task {codename!r} from {connection['host']}…")
    with tempfile.TemporaryDirectory(prefix="packer-solve-download-") as temporary:
        work = Path(temporary)
        downloaded = work / "package"
        env = environment()
        env.update({
            "SOLVE_HOST": connection["host"],
            "SOLVE_TOKEN": connection["token"],
            "XDG_CONFIG_HOME": str(work / "config"),
        })
        bootstrap = (
            "import requests; request=requests.request; "
            "requests.request=lambda *a,**kw: request(*a,**dict({'timeout':120},**kw)); "
            "from solve_cli import main; main()"
        )
        command = [
            str(runtime_python()), "-c", bootstrap, "task", "-c", codename,
            "-p", str(downloaded), "download",
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        manifest_path = downloaded / "config.json"
        if result.returncode != 0 or not manifest_path.is_file():
            raise RuntimeError(
                "Solve CLI did not download a complete package. Local project was not created."
            )
        try:
            manifest = load_manifest(downloaded)
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("Downloaded package has invalid config.json.") from error
        if not isinstance(manifest, dict):
            raise RuntimeError("Downloaded config.json is not a JSON object.")

        declared_language = manifest.get('default_language')
        language = language_code(declared_language) if declared_language else 'en'
        if not (downloaded / 'description' / f'{language}.md').is_file():
            supported = [p.stem.strip().lower() for p in sorted((downloaded / 'description').glob('*.md'))
                         if p.stem.strip().lower() in {'pl', 'en'}]
            language = supported[0] if supported else language
        ensure_new_project(store)
        prepare_drop_zones(codename)
        try:
            _copy_tree_without_symlinks(downloaded, destination)
        except BaseException:
            # The destination was verified empty above and belongs only to this
            # not-yet-created import, so rolling back its partial files is safe.
            checked_tree(destination)
            shutil.rmtree(destination, ignore_errors=True)
            raise

    descriptions = destination / "description"
    markdowns = sorted(descriptions.glob("*.md")) if descriptions.is_dir() else []
    images = [
        path.name for path in sorted(descriptions.iterdir())
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in IMAGE_SUFFIXES
    ] if descriptions.is_dir() else []
    setup: dict[str, object] = {
        "codename": codename,
        "has_package": True,
        "package_ready": True,
        "input_package": str(destination),
        "existing_tests": package_has_tests(codename),
        "language_code": language,
        "has_images": bool(images),
    }
    if markdowns:
        setup["statement_source"] = "package"
    if images:
        setup["images_ready"] = True
        setup["image_files"] = images
        setup["image_placements"] = {
            name: "keep the reference and placement from downloaded Markdown" for name in images
        }
    if manifest.get("type") == "solve-interactive":
        setup["task_type"] = "interactive"
    from .models import WorkflowState
    store.save(WorkflowState(setup=setup))
    print(
        f"Downloaded package to {destination.resolve()} and created a packer project. "
        "You can now complete the form and revise selected materials."
    )
    return store


def show_doctor() -> bool:
    data = doctor()
    heading("Solve environment")
    if data["ready"] and data["sandbox_ready"]:
        print("Ready.")
    elif not data["ready"] and data["sandbox_ready"] and data.get("wheel_error"):
        print("Basic workflows are ready; Solve features need attention.")
    else:
        print("Environment needs attention.")
    print(f"Python: {data['python']}")
    sandbox = data.get('sandbox') or 'missing — install the bubblewrap package'
    print(f"bubblewrap isolation: {sandbox}")
    if data.get('sandbox_ready'):
        print("  isolation test: OK")
    else:
        print("  isolation test: ERROR — " + data.get('sandbox_error', 'unknown error'))
    for name, version in data["versions"].items():
        print(f"  {name}: {version}")
    for name, path in data["compilers"].items():
        print(f"  {name}: {path or 'missing'}")
    if data.get("wheel_error"):
        print(data["wheel_error"])
    if data["error"]:
        print(data["error"])
    print("Interaction: local conversations available; native engine requires a jail server.")
    return bool(data["ready"] and data["sandbox_ready"])


def connection_path() -> Path:
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    )
    return config_home / "solve4-task-packer/solve-connection.json"


def _stored_connection(path: Path) -> dict:
    try:
        data = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    host = data.get("host")
    token = data.get("token")
    return {"host": host, "token": token} if host and token else {}


def configure_connection() -> dict:
    path = connection_path()
    old = _stored_connection(path)
    heading("Solve account configuration", "Each administrator completes this step only once.")
    print("Press Enter for the server address, then paste your private Solve API token.")
    host = ask("Solve server address", old.get("host", DEFAULT_SOLVE_HOST))
    parsed = urlparse(host)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise RuntimeError("Enter a full http/https URL without credentials in the address.")
    token = getpass.getpass("Solve API token (Enter keeps saved token): ") or old.get("token", "")
    if not token:
        raise RuntimeError("Solve token is missing.")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Make both temporary replacement and final file private.
    old_mask = os.umask(0o077)
    try:
        write_json(path, {"host": host.rstrip("/"), "token": token})
        path.chmod(0o600)
    finally:
        os.umask(old_mask)
    print(f"Ready. Private settings saved in: {path}")
    print("The token will not be displayed in reports or included in packages.")
    return {"host": host, "token": token}


def read_connection() -> dict:
    if os.environ.get("SOLVE_HOST") and os.environ.get("SOLVE_TOKEN"):
        return {"host": os.environ["SOLVE_HOST"], "token": os.environ["SOLVE_TOKEN"]}
    path = connection_path()
    saved = _stored_connection(path)
    if saved:
        return saved
    # Reuse the normal Solve CLI connection if configured already.
    standard = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "solve/solveconfig.json"
    if standard.is_file():
        data = _stored_connection(standard)
        if data:
            return data
    return configure_connection()


def cli_remote(config: ProjectConfig, action: str) -> None:
    if not doctor()["ready"]:
        raise RuntimeError("Run ./run.sh --doctor to prepare Solve CLI.")
    connection = read_connection()
    if action == "upload":
        result = check(config, reuse_native=True)
        if result["ok"] is not True:
            raise RuntimeError("Upload requires a successful package check.")
        archive = archive_package(config, expected=result.get('source_sha256'))
        print(f"Ready package: {archive.resolve()}\nTask: {config.codename}\nServer: {connection['host']}")
        if not ask_yes_no("Upload this package to the specified server?"):
            return
    else:
        result = check(config, reuse_native=True)
        if result['ok'] is not True:
            raise RuntimeError('Statement compilation requires a successful package check.')
        archive = archive_package(config, expected=result.get('source_sha256'))
        print(f"Statement and tests will be sent for PDF/HTML compilation at {connection['host']}.")
        if not ask_yes_no("Prepare a preview on the Solve server?"):
            return
    with tempfile.TemporaryDirectory(prefix="packer-solve-cli-") as temporary:
        work = Path(temporary)
        package = work / config.codename
        import zipfile
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(package)
        env = environment()
        env.update({"SOLVE_HOST": connection["host"], "SOLVE_TOKEN": connection["token"],
                    "XDG_CONFIG_HOME": str(work / "config")})
        bootstrap = ("import requests; request=requests.request; "
                     "requests.request=lambda *a,**kw: request(*a,**dict({'timeout':120},**kw)); "
                     "from libsolve.package import Package; Package.prepare=lambda *a,**kw: None; "
                     "from solve_cli import main; main()")
        command = [str(runtime_python()), "-c", bootstrap, "task", "-c", config.codename,
                   "-p", str(package), "upload" if action == "upload" else "description-compile"]
        if action != "upload":
            command += ["--all", "--force"]
        # CLI retains its own overwrite confirmation. Stream prompts immediately.
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        output = bytearray()
        try:
            while chunk := os.read(process.stdout.fileno(), 4096):
                output.extend(chunk)
                sys.stdout.write(chunk.decode(errors="replace"))
                sys.stdout.flush()
            code = process.wait()
        except BaseException:
            kill(process)
            raise
        text = output.decode(errors="replace")
        success = code == 0 and ("Done!" in text if action == "upload" else "Done" in text and "ERROR" not in text)
        if not success:
            raise RuntimeError("Solve CLI did not confirm the operation. Check the messages above.")
        if action != "upload":
            outputs = list((package / "description").glob("*.pdf")) + list((package / "description").glob("*.html"))
            if not outputs:
                raise RuntimeError("Server returned no preview files.")
            import shutil
            for path in outputs:
                target = safe_file(config.package_dir / "description", path.name)
                shutil.copy2(path, target)
                print(f"Preview: {target.resolve()}")
        else:
            print("Server confirmed package upload.")


def cli_rejudge(config: ProjectConfig) -> None:
    """Ask Solve CLI to rejudge all submissions for the remote task."""

    if not doctor()["ready"]:
        raise RuntimeError("Run ./run.sh --doctor to prepare Solve CLI.")
    connection = read_connection()
    print(
        f"Task: {config.codename}\nServer: {connection['host']}\n"
        "Solve will show the number of submissions and ask for rejudge confirmation."
    )
    with tempfile.TemporaryDirectory(prefix="packer-solve-rejudge-") as temporary:
        work = Path(temporary)
        env = environment()
        env.update({
            "SOLVE_HOST": connection["host"],
            "SOLVE_TOKEN": connection["token"],
            "XDG_CONFIG_HOME": str(work / "config"),
        })
        bootstrap = (
            "import requests; request=requests.request; "
            "requests.request=lambda *a,**kw: request(*a,**dict({'timeout':120},**kw)); "
            "from solve_cli import main; main()"
        )
        command = [
            str(runtime_python()), "-c", bootstrap,
            "task", "-c", config.codename, "rejudge",
        ]
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        output = bytearray()
        try:
            # os.read returns currently available pipe data, so the CLI's
            # confirmation prompt is visible before it waits for stdin.
            while chunk := os.read(process.stdout.fileno(), 4096):
                output.extend(chunk)
                sys.stdout.write(chunk.decode(errors="replace"))
                sys.stdout.flush()
            code = process.wait()
        except BaseException:
            kill(process)
            raise
        text = output.decode(errors="replace")
        if code == 0 and "Rejudge cancelled." in text:
            print("Rejudge cancelled; nothing was changed.")
            return
        if code != 0 or "Queued rejudge of" not in text or "Done" not in text:
            raise RuntimeError(
                "Solve CLI did not confirm the rejudge request. Check the messages above."
            )
        print("Solve accepted the submission rejudge request.")


def check(config: ProjectConfig, *, reuse_native: bool = False) -> dict:
    store = StateStore(config.codename)
    saved = store.load() if store.path.is_file() else None
    if saved and (saved.setup.get('review_pending') or saved.setup.get('pending_cleanup') or saved.drafts):
        raise RuntimeError('There are unapproved changes. Resume the project and finish review before checking or exporting.')
    usage = saved.usage if saved else None
    write_package_metadata(config, usage=usage)
    from .content_review import audit_is_current
    if not audit_is_current(config, store):
        from .console import project_command
        raise RuntimeError('No current statement-consistency check. Run:\n' +
                           project_command(config.codename, '--audit-statement') +
                           '\nThe audit and any statement revision use the API. --verify alone does not make API calls.')
    from .solve4 import local_validation
    from .freshness import changed_files, certify, certified_native_result, fingerprints
    from .verification import Verification
    errors = local_validation(config)
    if errors:
        raise RuntimeError('Package errors:\n' + '\n'.join(errors))
    local_snapshot = fingerprints(config.package_dir)
    changed = changed_files(config)
    if changed:
        print('Materials changed or are not yet checked; rerunning local checks.')
        report = Verification(config, usage=usage)
        try:
            report.run()
            report.record('Final result', 'PASS', 'Current materials were checked locally.')
            local_snapshot = report.verified_fingerprints
        except RuntimeError as error:
            report.record('Final result', 'FAIL', str(error))
            raise
    if reuse_native and not changed:
        cached = certified_native_result(config)
        if cached is not None:
            print('Solve: using current certified check result.')
            for item in cached.get("solutions", []):
                print(f"  {item['name']}: {item['score']:g}/100 pkt")
            print(f"Solve report: {(config.package_dir / 'verification/solve-check.json').resolve()}")
            return cached
    print("Solve: validation, compilation, and solution checking…")
    if fingerprints(config.package_dir) != local_snapshot:
        raise RuntimeError('Materials changed since local verification; rerun checks.')
    result = native_action(config)
    # Native results are part of the main report too, even when local materials
    # were unchanged and only Solve was rerun.
    Verification(config, usage=usage).load_existing().save()
    if result['ok'] is True:
        certify(config, native=True, expected=local_snapshot)
    for item in result.get("solutions", []):
        print(f"  {item['name']}: {item['score']:g}/100 pkt")
    print("Check completed." if result["ok"] is True else result.get("error", "Incorrect results detected. See the report."))
    if result.get("skipped"):
        print(result["skipped"])
    print(f"Solve report: {(config.package_dir / 'verification/solve-check.json').resolve()}")
    return result


def action(config: ProjectConfig, name: str) -> None:
    if name == "check":
        result = check(config)
        if result["ok"] is not True:
            raise RuntimeError("Solve check did not complete successfully.")
    elif name == "zip":
        result = check(config, reuse_native=True)
        if result["ok"] is not True:
            raise RuntimeError("Fix package errors first.")
        print(f"ZIP ready: {archive_package(config, expected=result.get('source_sha256')).resolve()}")
    elif name in {"preview", "upload"}:
        cli_remote(config, name)
    elif name == "rejudge":
        cli_rejudge(config)
    elif name == "connection":
        configure_connection()
    elif name == "report":
        report = checked_path(config.package_dir / "verification/report.md")
        print(report.read_text() if report.is_file() else "No local report exists yet.")
        native = checked_path(config.package_dir / "verification/solve-check.json")
        if native.is_file():
            data = json.loads(native.read_text())
            for row in data.get("solutions", []):
                print(f"{row['name']}: {row['score']:g}/100 pkt")
            print(f"Details: {native.resolve()}")


def menu(config: ProjectConfig) -> bool:
    while True:
        heading(f"Package {config.codename}", str(config.package_dir.resolve()))
        from .freshness import changed_files
        changed = changed_files(config)
        if changed:
            print('Check is outdated: ' + ', '.join(changed[:5]) + ('…' if len(changed)>5 else ''))
        selected = choose("What would you like to do?", {
            "r": "show report", "s": "build and check with Solve", "z": "prepare ZIP",
            "p": "PDF and HTML through Solve server", "w": "upload package", "k": "configure Solve connection",
            "j": "rejudge all submissions on the server",
            "e": "full edit, history, and deletion menu", "q": "exit",
        }, "q")
        if selected == "q":
            return False
        try:
            if selected == "e":
                from .revisions import edit_menu
                store = StateStore(config.codename)
                if edit_menu(store.load(), store):
                    return True
                continue
            action(config, {"r":"report", "s":"check", "z":"zip", "p":"preview", "w":"upload", "k":"connection", "j":"rejudge"}[selected])
        except RuntimeError as error:
            print(f"Failed: {error}")
