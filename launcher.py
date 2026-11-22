"""PyInstaller entrypoint wrapper for RemoteScreen."""

from remote_screen.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
