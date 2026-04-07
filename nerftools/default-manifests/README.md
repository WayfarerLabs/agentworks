# Default Manifests

These manifests ship with nerftools and are included automatically by the `nerf` CLI unless
`--no-default` is passed. Each `.yaml` file declares one package of tools.

To add your own tools, create a separate manifest file anywhere and pass it to the CLI:

```bash
nerf generate --target bin --outdir ./bin path/to/my-tools.yaml
```

Your manifest can define new packages or override individual tools in an existing package by using
the same `package.name`. See the [manifest reference](../docs/guides/nerf-manifest.md) for the
full format.
