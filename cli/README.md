# hardcode (CLI)

The command line for HardCode: profiles, allowlists, forwards, mounts and
languages. Python 3.9+, standard library only; YAML goes through mikefarah's
`yq` if it's installed, else PyYAML, else `yq`'s container image.

```sh
cli/hardcode profiles              # from a checkout, no install (with the checkout's stack)
pipx install ./cli                 # `hardcode` on your PATH (pipx install './cli[yaml]' adds PyYAML)
hardcode-linux-x64 profiles        # standalone binaries from the Releases page (macOS, Windows too)
```

Profiles are kept in the user's HardCode home (`~/.config/hardcode` on
Linux; `--config DIR` or `HARDCODE_CONFIG` for another). The stack the
command runs them with (`compose.yaml`, the images' build files, the
template) goes into the package when it is built (`setup.py`, and
`python -m hardcode_cli.stack DIR` for the PyInstaller builds), and is
written out to the cache folder the first time it runs.

Usage is in the top-level [README](../README.md#the-hardcode-command-cli);
`hardcode --help` and `hardcode <command> --help` have the details.

## Layout

```
hardcode_cli/cli.py    argparse, the command handlers, output (text or --json)
hardcode_cli/ops.py    the operations: allow, deny, forward, mount, enable, destroy …
hardcode_cli/allowlist.py  parse / validate / add / enable / remove entries
hardcode_cli/override.py   compose.override.yaml: forwards and mounts
hardcode_cli/profiles.py   list / create (copy) / remove, port and subnet allocation
hardcode_cli/envfile.py    .env read / get-with-defaults / set / unset (only what differs)
hardcode_cli/home.py       where the profiles are (the home), the cache, sudo refused
hardcode_cli/stack.py      compose.yaml, the images' build files and the template: found, written out, their defaults
hardcode_cli/compose.py    docker compose for a profile (injectable runner)
hardcode_cli/yamlio.py     the yq / PyYAML / Docker YAML backend
hardcode_cli/langs.py      the catalogue: languages, tools, the allowlist entries HardCode provides
tests/                 unittest: every public function, docker compose faked
```

```sh
python3 -m unittest discover -s cli -t cli     # unit tests, any OS
tests/cli.py                                   # + the command end to end against a throwaway home
```

A standalone binary: `app/build.sh cli` (it stages the stack into the package, then has PyInstaller carry it with `--add-data`). The package as a wheel, the stack inside: `app/build.sh wheel`.
