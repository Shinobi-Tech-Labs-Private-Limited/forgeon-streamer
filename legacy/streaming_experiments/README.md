# Legacy HLS streaming experiments

`hls.py` and `hls_l.py` are independent FastAPI/HLS/S3 streaming applications.
Neither file is imported by or required to run the production V13 Flask rig
application, and no direct launch was found in the inspected shell history.

They are retained for reference rather than deleted:

- `hls.py` — larger HLS/FastAPI implementation.
- `hls_l.py` — closely related HLS variant.

Do not treat either file as part of the V13 production launch chain without a
separate review of its configuration, network destinations, and credentials.
