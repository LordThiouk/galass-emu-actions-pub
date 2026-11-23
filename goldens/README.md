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
  PR qui modifie, supprime ou ajoute un golden est signalée, sa copie est
  remplacée par la référence, **et le run finit ROUGE** : toucher la référence
  depuis la branche est une tentative d'acceptation implicite, jamais un passage
  silencieux (même quand la comparaison, faite contre la référence restaurée,
  serait verte).
- **Harnais non falsifiable.** Les `*.dart` du dossier golden (test de capture,
  fixtures, harnais) sont eux aussi relus depuis la branche de base. Sans ça une
  branche pourrait neutraliser sa propre porte — un test remplacé par
  `void main() {}` fait sortir `flutter test` en « No tests ran », donc 0 écart,
  donc vert sans qu'aucune capture ne soit comparée. Toute modification ou ajout
  de `.dart` dans le dossier golden est signalé et fait échouer le run.
- **Aucun seuil caché.** La comparaison est celle de `flutter test` par défaut :
  égalité stricte des pixels, sans tolérance ni pourcentage.
- **Écart nommé.** Chaque échec sort sous la forme
  `Golden "<fichier>": Pixel test failed, X%, Npx diff detected`, et la planche
  affiche côte à côte référence / branche / pixels différents.

## Amorçage (bootstrap) : où vivent les goldens

La porte lit la référence dans `origin/<base_ref>`. Tant que
`test/goldens/<app>/` n'est pas **sur `main`** de l'app, `--base origin/main`
sort en **erreur 2** (`aucun golden sur origin/main`) — jamais en vert : une
référence absente n'est pas une absence d'écart. Trois façons de la faire
tourner avant la fusion du leg goldens :

```bash
# 1. la branche qui porte les goldens (celle du leg anti-régression)
--base origin/feat/multistop-rider-close-2026-11-20

# 2. une copie locale de main + goldens (worktree séparé, commit détaché)
git worktree add --detach /tmp/base origin/main
cp -r <branche>/test/goldens/rider /tmp/base/test/goldens/
git -C /tmp/base add test/goldens && git -C /tmp/base commit -m "baseline"
bash goldens/gate.sh /tmp/base --base "$(git -C /tmp/base rev-parse HEAD)"

# 3. après fusion : --base origin/main, le cas nominal
```

Une fois le leg goldens fusionné dans `main`, le paramètre par défaut
(`base_ref: main` dans le workflow) redevient le cas nominal et aucun
amorçage n'est nécessaire.

## Ce que la porte ne prouve pas

La référence étant **générée depuis `main`**, elle fige l'état de `main` — y
compris ses défauts déjà présents. La porte détecte un **changement** d'écran,
elle ne détecte pas un défaut antérieur à la référence : un écran déjà cassé sur
`main` est « conforme » à son golden. Corriger un tel défaut rend donc la porte
ROUGE et exige une acceptation explicite (c'est voulu : la revue voit la
planche). Les contrats d'écran (`test/widget/…contract_test.dart` côté app)
couvrent, eux, la présence attendue des éléments — ils peuvent être rouges sur
`main` et sur la branche, et c'est cette paire qui dit s'il y a régression ou
défaut préexistant.

Corollaire opérationnel : **vert ≠ « aucun défaut »**, vert = « aucun écart avec
la référence revue ». La phrase exacte autorisée dans un rapport est « zéro
écart vs la référence », jamais « aucune régression » sans le contrat d'écran.

## Lancer la porte en local

```bash
# dans le dépôt app, avec un remote qui contient la branche de base
bash harness/goldens/gate.sh . --name Galass_Rider \
  --goldens test/goldens/rider --base origin/main --out /tmp/board

# codes de sortie : 0 = VERT, 1 = ROUGE (écart OU référence/harnais touchés),
#                  2 = erreur d'exécution/compilation ou référence absente
```

La porte réécrit les PNG **et** les `*.dart` du dossier golden depuis la base
avant de lancer les tests : elle ne s'utilise pas sur un arbre où l'on est en
train d'éditer le harnais (lancer `flutter test` directement dans ce cas).

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

Trois chemins, tous explicites, aucun par défaut :

1. La porte échoue et publie `golden-diff-board` (planche avant/après).
2. La personne qui accepte relance avec `accept_new_goldens=true` **et** un
   `reason` non vide (ou un label nommé sur la PR) ; le run publie
   `golden-accepted/` avec les captures à committer.
3. Ces PNG sont commités dans `test/goldens/<app>/` **sur la branche de base**,
   avec la planche en référence de la revue.

Modifier ou supprimer un PNG, ou toucher un `.dart` du dossier golden depuis la
branche évaluée, ne passe jamais : la porte restaure la version de la base et
sort ROUGE. L'acceptation est donc toujours un commit revu sur la base, jamais
un effet de bord d'un run.

La porte est en lecture seule (`permissions: contents: read`) : elle n'écrit
jamais la référence elle-même. Un « accept » sans `reason` échoue avant tout run.

## Ajouter une app (Driver)

La porte est paramétrée par `--goldens` / `goldens_subdir` : `test/goldens/driver`
suffit, aucun changement de script. Les goldens Driver se génèrent depuis la
branche de base de `Galass_Driver` avec le même harnais, et se valident par un
`workflow_dispatch` avec `app_repo: LordThiouk/Galass_Driver` +
`goldens_subdir: test/goldens/driver`. Tant que `test/goldens/driver/` n'existe
pas côté Driver, la porte sort en **erreur 2** (`aucun golden`) — elle ne
« saute » pas l'app silencieusement.
