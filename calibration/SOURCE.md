# Calibration package provenance

This package was delivered to the rig in
`/home/shikhar/Downloads/rig-update-2026-08-17.zip` and described as vendored
from Forgeon `backend/app/calibration`.

The exact upstream Forgeon commit was not available in the local machine audit,
so no commit SHA is asserted here. The delivery archive SHA-256 is:

```text
caeee5a692c4ae7c45148b66b57061d94015974bc00a182764cd22294b873a6d
```

The files committed here, including `config.yaml`, were verified byte-for-byte
against that archive. `config.yaml` contains the physical cube's as-built face
rotations and paste offsets. Before changing this package, identify the current
upstream commit, compare both copies, and keep any required upstream change in a
separate Forgeon PR.
