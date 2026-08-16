Use the System plugins roster to discover the installed `apt` plugin and its configured state.
`agw resource list --kind apt-source --include-disabled --output json` and the corresponding
`apt-package` list show the catalog rows, including disabled rows. After authorization, enable it
exactly by adding `apt` to `[plugins].system` while preserving existing entries, then use the
separate verification action.

An apt package can name an apt source. Core applies that source before the package. A same-named
operator apt package still takes precedence, but a custom package that keeps a shipped source
dependency needs the `apt` plugin enabled. Alternatively, replace or remove that dependency too. The
next safe action is to review the listed catalog and choose the enablement action only for the
package/source combination your template needs.
