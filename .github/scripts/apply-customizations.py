#!/usr/bin/env python3
"""Apply custom Caddy modules and cosign signing on top of upstream.

This script is run by the sync-upstream workflow after resetting to
upstream/main.  It patches Dockerfiles to add extra xcaddy modules and
a custom CMD, then injects cosign image-signing steps into the reusable
build workflow.

Edit EXTRA_MODULES or CUSTOM_CMD below to change what gets overlaid.
"""

from __future__ import annotations

import pathlib
import sys

# ── Customisation knobs ──────────────────────────────────────────────
EXTRA_MODULES: list[str] = [
    "github.com/lucaslorentz/caddy-docker-proxy/v2",
    "github.com/mholt/caddy-l4",
]

CUSTOM_CMD: str = 'CMD ["caddy", "docker-proxy"]'
# ─────────────────────────────────────────────────────────────────────


def patch_dockerfile(path: pathlib.Path) -> None:
    """Inject extra ``--with`` modules and a custom CMD into *path*."""
    if not path.exists():
        print(f"  skip {path} (not found)")
        return

    text = path.read_text()

    # Bail out if already patched (idempotent)
    if all(mod in text for mod in EXTRA_MODULES) and CUSTOM_CMD in text:
        print(f"  {path} already patched")
        return

    lines = text.split("\n")

    # Locate the last --with line in the xcaddy build block
    last_with_idx = -1
    for i, line in enumerate(lines):
        if "--with " in line:
            last_with_idx = i

    if last_with_idx == -1:
        print(f"  WARNING: no --with lines in {path}, skipping")
        return

    # Ensure that line ends with a backslash continuation
    stripped = lines[last_with_idx].rstrip()
    if not stripped.endswith("\\"):
        lines[last_with_idx] = stripped + " \\"

    # Insert new modules that aren't already present
    new_mods = [m for m in EXTRA_MODULES if m not in text]
    for j, mod in enumerate(new_mods):
        is_last = j == len(new_mods) - 1
        suffix = "" if is_last else " \\"
        lines.insert(last_with_idx + 1 + j, f"    --with {mod}{suffix}")

    # Append CMD if missing
    joined = "\n".join(lines)
    if CUSTOM_CMD not in joined:
        while lines and lines[-1].strip() == "":
            lines.pop()
        lines.append("")
        lines.append(CUSTOM_CMD)
        lines.append("")

    path.write_text("\n".join(lines))
    print(f"  patched {path}")


# ── Cosign blocks (raw YAML destined for build-docker-image.yml) ─────
# These contain ${{ }} expressions that belong to the BUILD workflow,
# not the sync workflow.  Because this file is a .py script (not a
# workflow YAML), GitHub Actions will never evaluate them here.

COSIGN_SECRET_BLOCK = """\
      SIGNING_SECRET:
        required: false
"""

COSIGN_STEPS_BLOCK = """\

      - name: Install Cosign
        uses: sigstore/cosign-installer@v3
        if: github.event_name != 'pull_request'

      - name: Sign container image
        if: github.event_name != 'pull_request'
        run: |
          while IFS= read -r tag; do
            [ -z "$tag" ] && continue
            echo "Signing ${tag}..."
            cosign sign -y --key env://COSIGN_PRIVATE_KEY "${tag}" || echo "Warning: failed to sign ${tag}"
          done <<< "$TAGS"
        env:
          TAGS: ${{ steps.meta.outputs.tags }}
          COSIGN_PRIVATE_KEY: ${{ secrets.SIGNING_SECRET }}

"""

SIGNING_SECRET_PASS = "      SIGNING_SECRET: ${{ secrets.SIGNING_SECRET }}"


def patch_reusable_workflow() -> None:
    """Add cosign signing secret input and steps to the reusable build workflow."""
    wf = pathlib.Path(".github/workflows/build-docker-image.yml")
    if not wf.exists():
        print("  WARNING: reusable build workflow not found")
        return

    text = wf.read_text()

    if "cosign" in text.lower():
        print("  cosign already present in reusable workflow")
        return

    # 1. Add SIGNING_SECRET to the workflow_call secrets section.
    #    Find the last secret definition and append after it.
    lines = text.split("\n")
    last_secret_idx = -1
    for i, line in enumerate(lines):
        if line.strip() == "required: false" and i > 0:
            # Check context: a secret name at 6-space indent two lines up
            for j in range(max(0, i - 2), i):
                if lines[j].startswith("      ") and lines[j].strip().endswith(":"):
                    last_secret_idx = i
    if last_secret_idx != -1:
        for j, extra in enumerate(COSIGN_SECRET_BLOCK.rstrip("\n").split("\n")):
            lines.insert(last_secret_idx + 1 + j, extra)
        text = "\n".join(lines)
    else:
        print("  WARNING: could not find secrets section for SIGNING_SECRET injection")

    # 2. Add cosign steps before the "Verify built image" step
    verify_marker = "      - name: Verify built image"
    release_marker = "      - name: Create GitHub Release"
    notify_marker = "      - name: Notify on failure"

    if verify_marker in text:
        text = text.replace(verify_marker, COSIGN_STEPS_BLOCK + verify_marker)
    elif release_marker in text:
        text = text.replace(release_marker, COSIGN_STEPS_BLOCK + release_marker)
    elif notify_marker in text:
        text = text.replace(notify_marker, COSIGN_STEPS_BLOCK + notify_marker)
    else:
        print("  WARNING: could not find insertion point for cosign steps")
        return

    wf.write_text(text)
    print("  added cosign to reusable build workflow")


def patch_caller_workflows() -> None:
    """Pass SIGNING_SECRET through caller workflows to the reusable workflow."""
    for name in ("build-docker-image-standard.yml", "build-docker-image-alpine.yml"):
        wf = pathlib.Path(f".github/workflows/{name}")
        if not wf.exists():
            print(f"  skip {name} (not found)")
            continue

        text = wf.read_text()
        if "SIGNING_SECRET" in text:
            print(f"  SIGNING_SECRET already in {name}")
            continue

        lines = text.split("\n")
        inserted = False
        for i, line in enumerate(lines):
            if "DOCKERHUB_REPOSITORY_NAME" in line and "secrets." in line:
                lines.insert(i + 1, SIGNING_SECRET_PASS)
                inserted = True
                break

        if not inserted:
            # Fallback: look for the last line in a secrets block
            for i in range(len(lines) - 1, -1, -1):
                if "secrets." in lines[i] and "DOCKERHUB" in lines[i]:
                    lines.insert(i + 1, SIGNING_SECRET_PASS)
                    inserted = True
                    break

        if inserted:
            wf.write_text("\n".join(lines))
            print(f"  added SIGNING_SECRET to {name}")
        else:
            print(f"  WARNING: could not find insertion point in {name}")


def main() -> None:
    """Apply all customizations on top of upstream."""
    print("==> Patching Dockerfiles")
    patch_dockerfile(pathlib.Path("Dockerfile"))
    patch_dockerfile(pathlib.Path("Dockerfile.alpine"))

    print("==> Patching build workflows for cosign signing")
    patch_reusable_workflow()
    patch_caller_workflows()

    print("==> Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
