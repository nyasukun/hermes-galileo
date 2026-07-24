# Complete the hermes-galileo installation

Hermes installs the plugin source but does not install its Python dependencies.
Install this package into the same Python environment as Hermes:

```bash
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
uv pip install \
  --python ~/.hermes/hermes-agent/venv/bin/python \
  -e "$hermes_home/plugins/hermes_galileo"
```

If Hermes uses a different environment, replace the Python path above with
the interpreter that runs `hermes`. If `uv` is unavailable and that environment
contains pip, use `<hermes-python> -m pip install -e <plugin-directory>`.

The installer prompts for `GALILEO_API_KEY`, `GALILEO_PROJECT`, and
`GALILEO_LOG_STREAM` and saves them to the active `$HERMES_HOME/.env`.
When `HERMES_HOME` is unset, Hermes uses `~/.hermes`.
Keep the API key, Galileo routing values, and `HERMES_GALILEO_PSEUDONYM_SECRET` out of `config.yaml`.

To customize telemetry behavior, copy the tracked template:

```bash
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
cp "$hermes_home/plugins/hermes_galileo/config.yaml.example" \
  "$hermes_home/plugins/hermes_galileo/config.yaml"
```

Environment variables named `HERMES_GALILEO_*` override matching YAML fields.
Restart a running gateway after installing or changing either configuration surface.
