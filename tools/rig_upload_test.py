#!/usr/bin/env python3
"""Manual test of the Forgeon rig direct-upload flow (phase 2 API).
Runs with the Python stdlib only — no pip installs needed on the rig.

First run (mints a device token, prints it for reuse):
  python3 rig_upload_test.py --register --admin-email test.admin@example.com --admin-pass 'PASSWORD'

Upload (uses a real recorded file from the rig's disk):
  python3 rig_upload_test.py --token frg_rig_XXXX \
      --assessment <assessment_id> --instance 1 \
      --view side=sessions/session_.../recording_1/sync/cam1_sync_side.mp4 \
      --view front=sessions/session_.../recording_1/sync/cam2_sync_front.mp4 \
      --params '{"ball_speed": 120, "delivery_type": "normal"}'
"""
import argparse, json, os, sys, urllib.request, urllib.error

API = "https://api-dev-new.forgelabs.in/dev"

def call(method, url, data=None, headers=None, raw=False):
    body = data if raw else (json.dumps(data).encode() if data is not None else None)
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    if not raw and data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        print(f"  !! {method} {url.split('?')[0]} -> {e.code}: {detail}")
        sys.exit(1)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=API)
    ap.add_argument("--register", action="store_true", help="mint a new device token (admin login required)")
    ap.add_argument("--admin-email"); ap.add_argument("--admin-pass")
    ap.add_argument("--name", default="office-rig")
    ap.add_argument("--token", help="device token from a previous --register")
    ap.add_argument("--assessment"); ap.add_argument("--instance", type=int, default=1)
    ap.add_argument("--view", action="append", default=[], help="side=path.mp4 (repeatable: front=, back=, top=)")
    ap.add_argument("--params", default=None, help='JSON, e.g. {"ball_speed":120}')
    ap.add_argument("--activity", default="bowling")
    a = ap.parse_args()

    if a.register:
        assert a.admin_email and a.admin_pass, "--register needs --admin-email/--admin-pass"
        import urllib.parse
        form = urllib.parse.urlencode({"username": a.admin_email, "password": a.admin_pass}).encode()
        st, login = call("POST", f"{a.api}/login", form, {"Content-Type": "application/x-www-form-urlencoded"}, raw=True)
        H = {"Authorization": f"Bearer {login['access_token']}"}
        st, dev = call("POST", f"{a.api}/rig/devices", {"name": a.name}, H)
        print("Device registered:", dev["id"])
        print("DEVICE TOKEN (save it — shown once):", dev["device_token"])
        print("lan_token (for later, browser->rig):", dev["lan_token"])
        return

    assert a.token and a.assessment and a.view, "need --token, --assessment and at least one --view"
    DH = {"Authorization": f"Bearer {a.token}"}
    files = {}
    for spec in a.view:
        name, path = spec.split("=", 1)
        field = f"{name}_view"
        files[field] = path
        assert os.path.exists(path), f"missing file: {path}"

    manifest = [{"field": f, "filename": os.path.basename(p), "size_bytes": os.path.getsize(p)}
                for f, p in files.items()]
    print("[1] init:", [(m["field"], f"{m['size_bytes']/1e6:.1f} MB") for m in manifest])
    st, init = call("POST", f"{a.api}/rig/instances/init", {
        "assessment_id": a.assessment, "instance_no": a.instance, "files": manifest,
        "parameters": json.loads(a.params) if a.params else None,
        "activity_type": a.activity,
    }, DH)
    print("    instance_id:", init["instance_id"])

    print("[2] PUT bytes straight to GCS (resumable session URLs)")
    for field, spec in init["files"].items():
        with open(files[field], "rb") as fh:
            data = fh.read()
        st, _ = call("PUT", spec["upload_url"], data,
                     {"Content-Type": spec["content_type"]}, raw=True)
        print(f"    {field}: {len(data)/1e6:.1f} MB -> {st}")

    print("[3] complete")
    st, done = call("POST", f"{a.api}/rig/instances/complete", {
        "context_token": init["context_token"],
        "uploaded_fields": list(init["files"].keys()),
    }, DH)
    print("    ", json.dumps(done, indent=2))
    print("\nPASS — instance", done["instance_id"], "status:", done["status"])
    print("Check it in the admin UI under the assessment, or GET /technical-instances?assessment_id=...")

if __name__ == "__main__":
    main()
