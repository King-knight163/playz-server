import os
import uuid
import shutil
import zipfile
import boto3
import traceback
import subprocess
from flask import Flask, request, jsonify
from pathlib import Path
import resource
import sys

# ---------- Config from env ----------
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
S3_BUCKET = os.environ.get("S3_BUCKET")                 # must exist
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
API_KEY = os.environ.get("API_KEY")                    # simple auth token (recommended)
MAX_RUN_SECONDS = int(os.environ.get("MAX_RUN_SECONDS", "30"))
MAX_OUTPUT_BYTES = int(os.environ.get("MAX_OUTPUT_BYTES", "200000"))  # 200KB

if not (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY and S3_BUCKET):
    print("Missing S3 configuration env vars. Set AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and S3_BUCKET.")
    # But still allow local debug.

s3 = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=S3_REGION,
)

app = Flask(__name__)

BASE_WORKDIR = "/tmp/runs"
os.makedirs(BASE_WORKDIR, exist_ok=True)


def set_limits():
    """
    Called in child process (preexec_fn) to limit resources.
    CPU time (seconds) and memory (address space).
    """
    try:
        # limit CPU time
        resource.setrlimit(resource.RLIMIT_CPU, (MAX_RUN_SECONDS, MAX_RUN_SECONDS + 2))
        # limit address space (bytes) — e.g., 256MB
        mem_bytes = 256 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    except Exception:
        pass


def upload_to_s3(local_path: str, s3_key: str):
    s3.upload_file(local_path, S3_BUCKET, s3_key)
    # make public-read URL (depends on bucket policy); otherwise presign
    url = f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{s3_key}"
    return url


def safe_first_py(workdir: Path):
    # Prefer main.py -> app.py -> first *.py
    for name in ("main.py", "app.py"):
        p = workdir / name
        if p.exists():
            return p
    p_list = sorted(workdir.glob("*.py"))
    return p_list[0] if p_list else None


@app.route("/api/run", methods=["POST"])
def run_upload():
    # Simple auth (recommended)
    if API_KEY:
        key = request.headers.get("X-API-KEY") or request.form.get("api_key")
        if key != API_KEY:
            return jsonify({"error": "Unauthorized"}), 401

    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    upload = request.files["file"]
    filename = upload.filename or "file"
    run_id = uuid.uuid4().hex[:12]
    workdir = Path(BASE_WORKDIR) / run_id
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        # Save uploaded
        saved_path = workdir / filename
        upload.save(str(saved_path))

        # If zip -> extract
        if zipfile.is_zipfile(saved_path):
            with zipfile.ZipFile(saved_path, "r") as z:
                z.extractall(workdir)
            saved_path.unlink(missing_ok=True)
        # If uploaded a single .py, okay; if uploaded repo w/ requirements.txt, it will exist in workdir

        # Auto-install requirements if present
        req = workdir / "requirements.txt"
        venv_dir = workdir / ".venv"
        python_exec = sys.executable  # fallback to system python

        if req.exists():
            # create venv
            subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])
            pip_path = str(venv_dir / "bin" / "pip")
            python_exec = str(venv_dir / "bin" / "python")
            # upgrade pip then install
            subprocess.check_call([pip_path, "install", "--upgrade", "pip", "setuptools", "wheel"])
            subprocess.check_call([pip_path, "install", "-r", str(req)])

        # Determine which file to run
        entry = None
        # If client provided entry param, use it
        if "entry" in request.form:
            candidate = workdir / request.form["entry"]
            if candidate.exists():
                entry = candidate
        if entry is None:
            entry = safe_first_py(workdir)

        if entry is None:
            return jsonify({"error": "No Python entrypoint found (upload main.py or specify entry form field)"}), 400

        # prepare output file
        output_file = workdir / "output.txt"

        # Run with resource limits and timeout
        try:
            proc = subprocess.run(
                [python_exec, str(entry)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=MAX_RUN_SECONDS,
                preexec_fn=set_limits
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            combined = ""
            if proc.returncode == 0:
                combined = stdout
            else:
                combined = "=== STDOUT ===\n" + stdout + "\n\n=== STDERR ===\n" + stderr + f"\n\nExitCode: {proc.returncode}"

        except subprocess.TimeoutExpired as te:
            combined = f"TimeoutExpired: exceeded {MAX_RUN_SECONDS} seconds.\nPartial output:\n{te.stdout}\n{te.stderr}"

        except Exception as e:
            combined = "Exception during run:\n" + traceback.format_exc()

        # truncate large output
        if len(combined) > MAX_OUTPUT_BYTES:
            combined = combined[:MAX_OUTPUT_BYTES] + "\n\n...OUTPUT_TRUNCATED..."

        output_file.write_text(combined, encoding="utf-8")

        # upload the uploaded files (optional) and output to S3
        s3_key_output = f"outputs/{run_id}.txt"
        output_url = upload_to_s3(str(output_file), s3_key_output)

        # Upload original uploaded bundle for reference (zip it)
        bundle_path = workdir / "bundle.zip"
        shutil.make_archive(str(bundle_path.with_suffix("")), 'zip', str(workdir))
        s3_key_bundle = f"bundles/{run_id}.zip"
        bundle_url = upload_to_s3(str(bundle_path), s3_key_bundle)

        return jsonify({
            "status": "done",
            "run_id": run_id,
            "output_url": output_url,
            "bundle_url": bundle_url,
        })

    except subprocess.CalledProcessError as cpe:
        return jsonify({"error": "Dependency install or setup failed", "detail": str(cpe)}), 500

    except Exception as ex:
        tb = traceback.format_exc()
        return jsonify({"error": "Server Error", "detail": tb}), 500

    finally:
        # keep workdir for debugging for a short while; you can delete it here if you want
        # shutil.rmtree(workdir, ignore_errors=True)
        pass


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
