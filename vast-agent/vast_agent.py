#!/usr/bin/env python3
"""
Delegator: keep this small to preserve existing invocation paths.

The full controller implementation lives in the packaged module
`vast_agent.vast_agent`. This file only delegates to that module and
exits with an informative error if delegation fails.
"""
import runpy
import sys


def main():
    try:
        # Try to import and call main() from the package if available
        pkg = __import__('vast_agent.vast_agent', fromlist=['main'])
        if hasattr(pkg, 'main'):
            pkg.main()
            return
        # Fallback: run the module as __main__
        sys.argv[0] = 'vast_agent.vast_agent'
        runpy.run_module('vast_agent.vast_agent', run_name='__main__')
    except Exception as e:
        sys.stderr.write(f"Error: failed to launch vast_agent.vast_agent: {e}\n")
        raise SystemExit(1)


if __name__ == '__main__':
    main()
        except Exception as e:
            log.warning(f"Failed to provision runner user-data: {e}")
        finally:
            # Best-effort cleanup of temp file
            try:
                if tmp_out and Path(tmp_out).exists():
                    Path(tmp_out).unlink()
            except Exception:
                pass

    # IMAGE_MAP: optional env mapping keys to image tags. Format: "key1=ghcr.io/org/repo:tag1,key2=ghcr.io/org/repo:tag2"
    IMAGE_MAP_RAW = os.getenv("IMAGE_MAP", "")
    if IMAGE_MAP_RAW and offer:
        try:
            # parse into dict
            m = dict(x.split("=", 1) for x in IMAGE_MAP_RAW.split(",") if "=" in x)
            offer_text = str(offer)
            for k, v in m.items():
                if k.strip() and k.strip().lower() in offer_text.lower():
                    selected_image = v.strip()
                    log.info(f"Selecting image from IMAGE_MAP: key={k} -> {selected_image}")
                    break
        except Exception as e:
            log.warning(f"Failed to parse IMAGE_MAP: {e}")

    # verify accessibility for registry-hosted images (e.g. ghcr.io)
    fallback_img = os.getenv("VAST_FALLBACK_IMAGE", "ubuntu:22.04")
    try:
        if selected_image and ('ghcr.io' in selected_image or ('/' in selected_image and '.' in selected_image.split('/')[0])):
            ok = _head_manifest(selected_image)
            if not ok:
                log.warning(f"Image {selected_image} not publicly accessible; falling back to {fallback_img}")
                selected_image = fallback_img
    except Exception as e:
        log.warning(f"Image accessibility check failed: {e}; proceeding with requested image")

    # Enforce image-only provisioning: include selected_image or fallback.
    if selected_image:
        body['image'] = selected_image
    #!/usr/bin/env python3
    """Small delegator for backwards-compatible invocation.

    This file intentionally keeps no provisioning logic. It simply imports
    the packaged controller `vast_agent.vast_agent` and calls its `main()`
    function so older invocation paths (e.g., `python vast-agent/vast_agent.py`)
    continue to work while all real logic lives in the package.
    """
    import sys


    def main() -> int:
        try:
            # Import the packaged main and run it. The packaged module enforces
            # image-only provisioning and provides a programmatic `main(argv)`
            # entrypoint that returns an int exit code.
            pkg = __import__("vast_agent.vast_agent", fromlist=["main"])  # type: ignore
            if hasattr(pkg, "main"):
                return pkg.main()
            # Fallback: if main isn't present, fail explicitly.
            sys.stderr.write("Error: packaged module vast_agent.vast_agent has no main()\n")
            return 1
        except Exception as e:  # pragma: no cover - defensive
            sys.stderr.write(f"Error: failed to launch vast_agent.vast_agent: {e}\n")
            return 2


    if __name__ == "__main__":
        raise SystemExit(main())
            "REDIS_URL": REDIS_URL,
