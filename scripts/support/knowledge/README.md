# Knowledge base

A single, easy-to-read home for **user-facing documentation** about how glidearr works —
written for the people who run the server and the families who use it, not just developers.

## What's here

- **[personalized-playlists.md](personalized-playlists.md)** — How the per-profile "Up Next"
  playlists, the age-tiered family collections, and the library classification work; what
  each profile sees; how age restrictions are decided; safety/privacy; and how to turn it on.

## Read every doc in one place — `_mirror/`

To browse **all** of the repository's Markdown in one folder, run:

```bash
python scripts/support/tools/mirror_docs.py
```

This copies **every `.md` in the repo, byte-for-byte**, into `_mirror/` (next to this README),
**leaving the originals in place**. Each copy is named by its source path (separators flattened
to `__`) so docs from different folders never collide, and a generated `_mirror/INDEX.md` maps
every copy back to its source. Re-run it any time — it rebuilds `_mirror/` from scratch, so the
copies always match the current originals and stale ones disappear.

- Originals are never moved or changed — edit those, not the mirror.
- The `_mirror/` folder is a **derived artifact**; gitignore it if you'd rather not commit it.
- See [`scripts/support/tools/mirror_docs.py`](../tools/mirror_docs.py) (supports `--dry-run`).
