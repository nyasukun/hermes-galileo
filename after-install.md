# Complete the hermes-galileo installation

Hermes installs the plugin source but does not install its Python dependencies.
Install this package into the same Python environment as Hermes:

```bash
uv pip install \
  --python ~/.hermes/hermes-agent/venv/bin/python \
  -e ~/.hermes/plugins/hermes_galileo
```

If Hermes uses a different environment, replace the Python path above with
the interpreter that runs `hermes`. If `uv` is unavailable and that environment
contains pip, use `<hermes-python> -m pip install -e <plugin-directory>`.

The installer prompts for `GALILEO_API_KEY`, `GALILEO_PROJECT`, and
`GALILEO_LOG_STREAM` and saves them to `~/.hermes/.env`. Restart a running
gateway after installing or changing configuration.
