# Porte anti-régression visuelle (golden-diff)

Une branche Rider/Driver ne peut plus modifier un écran sans que l'écart soit
visible : ses écrans sont comparés aux **goldens officiels** de sa branche de
base, en égalité stricte de pixels, et toute différence fait échouer la porte.

| pièce | rôle |
|---|---|
| `goldens/gate.sh` | la porte : restaure la référence depuis la branche de base, lance `flutter test`, écrit le verdict |
| `goldens/board.py` | la planche avant/après (`index.html` autonome + `summary.md` + `gaps.json`) |
| `.github/workflows/golden-diff.yml` | le job CI (dépôt public ; les dépôts app sont privés et sans Actions) |
| `test/goldens/<app>/` côté app | les goldens commités + le harnais de capture |

## Ce que la porte garantit

- **Référence non falsifiable.** Les PNG de référence sont relus depuis la
  branche de base (`origin/<base_ref>`), jamais depuis la branche évaluée. Une
  PR qui modifie, supprime ou ajoute un golden est signalée, et sa copie est
  remplacée par la référence ; un golden absent de la base compte comme
  **nouvelle référence** → échec, jamais un passage silencieux.
- **Aucun seuil caché.** La comparaison est celle de `flutter test` par défaut :
  égalité stricte des pixels, sans tolérance ni pourcentage.
- **Écart nommé.** Chaque échec sort sous la forme
  `Golden "<fichier>": Pixel test failed, X%, Npx diff detected`, et la planche
  affiche côte à côte référence / branche / pixels différents.

## Lancer la porte en local

```bash
# dans le dépôt app, avec un remote qui contient la branche de base
bash harness/goldens/gate.sh . --name Galass_Rider \
  --goldens test/goldens/rider --base origin/main --out /tmp/board

# codes de sortie : 0 = VERT, 1 = ROUGE, 2 = erreur d'exécution/compilation
```

`--accept` produit `board/accepted-goldens/` (les captures à committer) au lieu
d'échouer — à n'utiliser que pour une acceptation volontaire.

## Déclencher en CI

```bash
gh workflow run golden-diff.yml -R LordThiouk/galass-emu-actions-pub \
  -f app_repo=LordThiouk/Galass_Rider \
  -f ref=feat/ma-branche -f base_ref=main
```

Ou par `repository_dispatch` (chemin utilisé par `galass-ci`, qui déclenche déjà
les jobs sur les dépôts privés) :

```json
{"event_type":"golden-diff",
 "client_payload":{"app_repo":"LordThiouk/Galass_Rider","ref":"feat/ma-branche",
                   "base_ref":"main","goldens":"test/goldens/rider"}}
```

## Accepter une nouvelle référence (jamais implicite)

1. La porte échoue et publie `golden-diff-board` (planche avant/après).
2. La personne qui accepte relance avec `accept_new_goldens=true` **et** un
   `reason` non vide ; le run publie `golden-accepted/`.
3. Ces PNG sont commités dans `test/goldens/<app>/` sur la branche de base, avec
   la planche en référence de la revue.

La porte est en lecture seule (`permissions: contents: read`) : elle n'écrit
jamais la référence elle-même. Un « accept » sans `reason` échoue avant tout run.

## Ajouter une app (Driver)

La porte est paramétrée par `--goldens` / `goldens_subdir` : `test/goldens/driver`
suffit, aucun changement de script. Les goldens Driver se génèrent depuis la
branche de base de `Galass_Driver` avec le même harnais.
