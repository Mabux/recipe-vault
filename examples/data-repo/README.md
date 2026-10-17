# Recipe data repo (template)

Starting point for your **private** recipe repository. The web app reads recipes from it and the importer commits new recipes to it. Keep it private: imported recipes and photos usually belong to their original authors.

```
recipes/<year>/<yyyy-mm-dd>-<slug>.md   one Markdown file per recipe
images/<slug>-<hash>.webp               photos (optional, referenced by `image:`)
INDEX.md                                A–Z list of all recipes (generated)
IMPORT-REPORT.md                        skipped/failed imports (written by the importer)
seed-done.txt                           optional: inbox files already imported by hand
scripts/build_index.py                  copy of ../../scripts/build_index.py (see below)
.githooks/pre-commit                    keeps INDEX.md in sync on local commits
.gitea/workflows/index.yml              keeps INDEX.md in sync on every push
```

## Set up

1. Copy this folder somewhere outside the code repo and add the index script:

   ```bash
   cp -r examples/data-repo ~/recipe-vault-data
   ```

   ```bash
   mkdir -p ~/recipe-vault-data/scripts && cp scripts/build_index.py ~/recipe-vault-data/scripts/
   ```

2. Turn it into a Git repo and enable the hook:

   ```bash
   cd ~/recipe-vault-data && git init -b main && git config core.hooksPath .githooks
   ```

3. Create an empty **private** repository in Gitea (e.g. `recipe-vault-data`), then push:

   ```bash
   git add . && git commit -m "Initial recipe data" && git remote add origin http://192.168.1.10:3000/your-user/recipe-vault-data.git && git push -u origin main
   ```

4. Point the app (`GITEA_REPO=your-user/recipe-vault-data`) and the importer (`REPO_URL=ssh://…/recipe-vault-data.git`) at it.

The two sample recipes show the format; delete them once you have your own. The format is described in the main [README](../../README.md#recipe-file-format).

## INDEX.md workflow (optional)

`.gitea/workflows/index.yml` regenerates `INDEX.md` when someone pushes recipe changes without the hook (e.g. edits in the Gitea web UI). The importer updates `INDEX.md` itself, so this is only needed for manual edits. It needs:

- a Gitea Actions runner with the label `host` (plain shell, no Docker),
- a repository secret `GITEA_TOKEN` with write access to this repo,
- optionally a repository variable `CLONE_BASE_URL` if the runner should clone via a different address than Gitea's `ROOT_URL` (e.g. `http://192.168.1.10:3000`).
