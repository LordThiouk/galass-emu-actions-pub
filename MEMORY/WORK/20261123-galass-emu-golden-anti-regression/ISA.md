---
task: "Galass emu — harnais e2e : repère invisible stable + planche goldens + porte CI anti-régression"
slug: 20261123-galass-emu-golden-anti-regression
project: galass-emu-actions-pub
isa_kind: task
phase: plan
progress: 1/4
algorithm: 8.20.2
isa_spec: 2.21.0
started: 2026-11-23T12:00:00Z
updated: 2026-11-23T06:23:46.563Z
parent: null
principal_stated_goal: "Méthode anti-régression UI multistop — harnais emu : repère invisible stable (remplace les compteurs x/N textuels), planche e2e branche vs main, validation finale multistop (arrêts + contrats d'écran + zéro écart golden non accepté)"
principal_stated_goal_source: principal
principal_stated_goal_signal: 5
principal_stated_goal_locked: 2026-11-23T12:00:00Z
context_sufficient: true
interview_invoked: false
isa_stamp: hmac-sha256:f9129b82c254a4f59b608be1d23d3a216eb164a4a4e41d23514a95faba7c7c76
---

## Problem

Le harnais e2e (`e2e/run.sh`) détecte la progression des arrêts par **texte affiché** : `MARK_rider_stop1_done='+1/2 arrêts? terminés?&...'`, `MARK_rider_stop2_done='+2/2 arrêts? terminés?&...'`, `MARK_driver_stop1_done='+1/2 arrêts? terminés?|...'`, `MARK_driver_stop2_done='+2/2 arrêts? terminés?|...'` (confirmé par lecture, `e2e/run.sh` l.117-118, l.145-146). Les ISA Rider (`ISC-F4-22`) et Driver (`ISC-F5-14`) retirent ce texte de l'UI (méthode anti-régression 2026-11-23) : une fois ce correctif livré, ces 4 marqueurs ne matcheront plus **rien**, et le harnais ne peut aujourd'hui pas distinguer « régression » de « marqueur devenu obsolète par un retrait volontaire ». Par ailleurs, aucune comparaison golden n'existe : la revue UI du 2026-11-23 (F10 Rider, F6 Driver) s'est faite à l'œil sur des captures e2e ponctuelles, sans référence stable ni porte bloquante par PR.

## Vision

Le harnais lit un repère **stable et invisible** (clé/Semantics sur le nœud actif ou l'état course, posé par ISC-F4-22/F5-14) à la place du texte du compteur — la détection de progression des arrêts survit au retrait du compteur sans réécrire le harnais à chaque changement de libellé. Chaque PR sur Rider/Driver déclenche une comparaison de ses écrans aux **goldens officiels** (générés depuis `main`, commités dans `test/goldens/` de chaque app) ; tout écart fait échouer la porte, sans seuil caché, et une nouvelle référence ne s'accepte que par une commande ou un label nommé explicitement, avec planche avant/après en artefact. La validation finale du multistop est une e2e verte qui couvre les arrêts, les contrats d'écran (Rider `ISC-F4-29`, Driver `ISC-F5-17`) et zéro écart golden non accepté.

## Out of Scope

- Correctif des écrans produit Rider/Driver eux-mêmes (ISA Rider F11 / Driver F6 enrichi, pas ce dépôt).
- Génération des fichiers golden image eux-mêmes (dépend de l'infra `test/goldens/` dans les repos Rider/Driver, pas encore construite — cf. Decisions).
- Changement du secret/token CI, des runners, ou du plan GitHub Actions.
- `galass-ci` (dépôt séparé, hors périmètre de cette ISA sauf référence en Language/Decisions).

## Language

- **repère invisible stable** — `Key`/`Semantics` posé sur le nœud actif ou l'état course par l'app (ISC-F4-22 Rider, ISC-F5-14 Driver), lisible par `uiautomator dump` sans dépendre du texte affiché. *Avoid* : confondre avec un simple `Key` de debug non garanti en prod.
- **golden** — capture de référence officielle d'un écran dans un état donné, générée depuis `main` dans un worktree séparé, avec données de test fixes et appareil fixe ; commitée dans `test/goldens/<app>/` avec une ligne README (écran, état, commit `main` d'origine).
- **porte CI golden-diff** — job public qui compare les écrans d'une PR aux goldens et échoue la PR sur tout écart, sans seuil de tolérance caché ; une nouvelle référence ne s'accepte que par commande/label nommé, avec planche avant/après en artefact.

## Constraints

- Pas d'Actions GitHub sur les dépôts privés (`Galass_Rider`, `Galass_Driver`, `Galass_backend`) — toute porte CI tourne sur un dépôt public (`galass-emu-actions-pub` et/ou `galass-ci`), jamais sur le dépôt cible.
- Aucun secret recopié dans ce dépôt ni dans l'ISA — lecture du bundle evidence déchiffré autorisée, jamais sa recopie.
- Aucune phase changée à la main, aucun ISC coché sans preuve, aucun gate affaibli.
- `gitleaks` avant chaque push ; push sur la branche nommée uniquement, sans merge, **sans e2e** pour ce tour (2026-11-23).

## Goal

Un harnais qui (1) survit au retrait des compteurs x/N sans perdre la détection de progression des arrêts, (2) peut comparer toute PR Rider/Driver à une planche golden officielle sans seuil caché, et (3) donne un verdict final multistop unique qui couvre arrêts + contrats d'écran + zéro écart golden non accepté.

## Decisions

- **2026-11-23 — Scaffold initial (Papa, méthode anti-régression UI, ISA-only, aucun code produit).** Grounding : `e2e/run.sh` l.117-118 (Rider) et l.145-146 (Driver) confirment que `MARK_*_stop1_done`/`MARK_*_stop2_done` matchent aujourd'hui le texte « x/2 arrêt(s) terminé(s) » que les ISA Rider (ISC-F4-22) et Driver (ISC-F5-14) retirent — sans ce leg, le retrait casserait silencieusement la détection e2e des arrêts. Aucune ISA n'existait pour ce dépôt (`MEMORY/WORK/` absent avant ce tour) ; slug et structure alignés sur le format des ISA Rider/Driver/backend pour cohérence inter-dépôts, bien qu'aucune convention locale n'existe encore ici.
- **2026-11-23 — Porte CI golden-diff : emplacement non tranché, décision à confirmer par Papa.** Deux dépôts publics existent déjà : `galass-ci` (déclenche des jobs opencode/FullRun sur les dépôts Galass via `GALASS_TOKEN`, lit `opencode/tests/` + gitleaks sur lui-même) et `galass-emu-actions-pub` (héberge l'e2e émulateur, `artemis-staging-e2e.yml`, déclenché par `galass-ci`). **Inférence non construite** : la porte golden-diff s'intègre le plus naturellement comme un nouveau job dans `galass-emu-actions-pub` (où les captures d'écran existent déjà), déclenché par PR Rider/Driver via le même mécanisme que `galass-ci` utilise déjà pour FullRun — mais ceci n'a pas été construit ni confirmé ce tour : écrire le workflow YAML, le déclencheur cross-repo et les secrets sans validation de Papa risquerait de consommer des minutes CI sur une architecture non voulue. **Point à trancher avec Papa avant tout code CI.**
- **2026-11-23 — Infra goldens (`test/goldens/`) : non construite ce tour.** Aucun `golden_toolkit` ni `test/goldens/` n'existe dans `Galass_Rider` ni `Galass_Driver` (confirmé par recherche dans les deux repos) — construire les harnais widget-test à données/appareil fixes pour ~8 écrans × 2 apps est un effort de plusieurs légs, pas fait dans cette passe ISA-only. Documenté ici comme dépendance plutôt que bâti à la hâte sans worktree `main` séparé ni confirmation de portée (quels écrans en premier).
- **2026-11-23 — Porte golden-diff construite et prouvée (run CI build-only, branche `feat/multistop-rider-close-2026-11-20`).** Emplacement confirmé par Papa : `galass-emu-actions-pub`. Trois pièces : `goldens/gate.sh` (la porte), `goldens/board.py` (planche avant/après autonome + `summary.md` + `gaps.json`), `.github/workflows/golden-diff.yml` (`workflow_dispatch` + `repository_dispatch`, `permissions: contents: read`, job `timeout-minutes: 30`). L'app porte ses goldens dans `test/goldens/<app>/` — pour Rider, 10 PNG générés depuis `origin/main` (`7984da7`) dans un worktree séparé, commités avec harnais et fixtures synthétiques.
- **2026-11-23 — Deux propriétés de non-falsifiabilité ajoutées après mesure, pas par principe.** (1) Référence : les PNG sont relus depuis la branche de base, et un PNG/`*.dart` modifié, supprimé ou ajouté par la branche fait désormais échouer le run (`exit 1`) au lieu d'être seulement signalé — mesuré : `cp accueil.png choix_service.png` dans l'arbre évalué → ROUGE avec la ligne `M test/goldens/rider/choix_service.png` et comparaison faite contre la référence restaurée. (2) Harnais : les `*.dart` du dossier golden sont eux aussi relus depuis la base. Sans ça la porte était contournable en une ligne — mesuré : les trois tests remplacés par `void main() {}` donnent `flutter test` = « No tests ran » (0 test, 0 écart, donc VERT sans qu'aucune capture ne soit comparée) ; avec la restauration, 10 tests tournent et le run sort ROUGE pour harnais modifié.
- **2026-11-23 — Amorçage (bootstrap) : la porte ne peut pas être verte sans goldens sur la référence.** `--base origin/main` sur Rider sort en `exit 2` (« aucun golden sur origin/main dans test/goldens/rider ») tant que le leg goldens n'est pas fusionné — c'est voulu : une référence absente n'est pas une absence d'écart. Les trois chemins documentés (`goldens/README.md` § Amorçage) : branche portant les goldens, worktree `main` + goldens en commit détaché, puis `origin/main` après fusion. Le workflow reçoit `base_ref` en paramètre pour couvrir les deux premiers.
- **2026-11-23 — Preuve à deux runs (Flutter 3.44.8, surface 412×915@2, `fr_SN`).** Base = `origin/main` `7984da7` + goldens (commit détaché `08cc8cd` dans un worktree séparé) : **VERT 10/10, 0 écart**. Branche Rider `feat/multistop-rider-close-2026-11-20` (`47a2ee2`) : **ROUGE 5/10** — `course_en_cours_stop1_actif` 27,33 %, `tous_arrets_faits` 27,33 %, `stop1_fait` 27,31 %, `course_en_cours_simple` 19,20 %, `course_terminee` 8,31 %. `--accept` sur le run rouge exporte 5 nouvelles références dans `accepted-goldens/` sans toucher la référence (exit 0, aucune écriture dans `test/goldens/`).
- **2026-11-23 — Le rouge ne nomme qu'une des quatre régressions attendues, et c'est une mesure, pas un choix.** Les goldens venant de `main`, ils figent l'état de `main`. Contrat d'écran Rider (`test/widget/course_en_cours_contract_test.dart`, mêmes fixtures) mesuré sur les deux arbres : ISC-F4-25 PASS sur `main` / FAIL sur la branche (régression introduite par la branche → c'est elle que l'écart pixel de `course_en_cours_simple` révèle) ; ISC-F4-26 FAIL/FAIL et ISC-F4-27 FAIL/FAIL (défauts **préexistants** sur `main`, donc figés par la référence, invisibles en diff) ; ISC-F4-28 PASS/PASS (aucune disparition du SOS mesurée en `RIDE_RUNNING` — l'écart Papa/code porte donc sur un autre état que RUNNING). Conséquence écrite dans les deux README : « vert = zéro écart vs la référence », jamais « aucune régression ».

## Features

### F1 · Repère invisible stable (remplace les marqueurs texte x/N)

- [ ] ISC-EMU-1: `e2e/run.sh` lit un repère stable à la place du texte « x/2 arrêt(s) terminé(s) » pour `MARK_rider_stop1_done`, `MARK_rider_stop2_done`, `MARK_driver_stop1_done`, `MARK_driver_stop2_done`. **Contrat fixé 2026-11-23 (leg Vera, portée sur cette branche)** : Rider pose UN nœud `Semantics` invisible, `content-desc` exact `galass.rider.stops=<done>/<total>`, monté seulement si `ride.stops` non vide (`ISC-F4-22`). Driver pose le miroir `galass.driver.stops=<done>/<total>` (`ISC-F5-14`). Harnais lit `content-desc` via `uiautomator dump` (un `Key` Flutter seul ne suffit pas). **Bloqué implémentation** : les apps n'exposent pas encore ce repère ; aucun code `e2e/run.sh` modifié dans ce leg Vera. [app: galass-emu-actions-pub · classe: nouvelle fonctionnalité, contrat fixé, implémentation bloquée · preuve: 4 marqueurs e2e verts sur une app de test avec repère posé]

### F2 · Planche e2e branche vs main

- [ ] ISC-EMU-2: le rapport de run (`e2e/report.py` ou équivalent) produit une planche comparant les captures e2e de la branche de travail aux captures d'un run équivalent sur `main`, par écran/état. [app: galass-emu-actions-pub · classe: nouvelle fonctionnalité · preuve: artefact planche généré sur 1 run de test]

### F3 · Validation finale multistop

- [ ] ISC-EMU-3: un verdict e2e unique couvre (a) la progression des arrêts (via ISC-EMU-1), (b) les contrats d'écran Rider (`ISC-F4-29`) et Driver (`ISC-F5-17`) — présence des éléments obligatoires capturée en e2e, pas seulement en widget test local — et (c) zéro écart golden non accepté (dépend de F4/porte CI). [app: galass-emu-actions-pub · classe: nouvelle fonctionnalité, dépend ISC-F4-29/F5-17 + porte CI · preuve: 1 run e2e vert couvrant les 3 volets]

### F4 · Porte CI golden-diff (dépôt à confirmer)

- [x] ISC-EMU-4: une porte CI compare les écrans d'une PR Rider/Driver aux goldens officiels (`test/goldens/`), échoue sur tout écart sans seuil caché, et n'accepte une nouvelle référence que par commande/label nommé avec planche avant/après en artefact. **Bloqué** : emplacement (`galass-emu-actions-pub` vs nouveau workflow `galass-ci`) non confirmé par Papa (cf. Decisions) ; infra `test/goldens/` elle-même pas construite dans Rider/Driver. [app: galass-emu-actions-pub (ou galass-ci) · classe: nouvelle fonctionnalité, deux dépendances bloquantes · preuve: 1 PR de test avec écart volontaire → porte rouge ; 1 PR sans écart → porte verte]

## Test Strategy

| isc | type | check | threshold | tool | anchors_to |
|-----|------|-------|-----------|------|------------|
| ISC-EMU-1 | bash | `e2e/run.sh` — les 4 marqueurs `*_stop1_done`/`*_stop2_done` matchent le repère stable, pas le texte compteur | 4/4 marqueurs résolus sur un run avec compteur retiré | e2e run.sh sur build de test (ISC-F4-22/F5-14 livrés) | ISC-F4-22, ISC-F5-14 |
| ISC-EMU-2 | bash | génération de la planche branche-vs-main | artefact produit, non vide | python3 e2e/report.py (ou nouveau script planche) | literal |
| ISC-EMU-3 | integration | run e2e couvrant arrêts + contrats d'écran + zéro écart golden non accepté | verdict global PASS | galass-emu-actions-pub run | ISC-F4-29, ISC-F5-17, ISC-EMU-4 |
| ISC-EMU-4 | integration | porte CI golden-diff sur PR de test | rouge sur écart volontaire, vert sinon | workflow CI (dépôt à confirmer) | literal — emplacement à confirmer |

## Learning

(Aucune entrée — scaffold initial.)
- **Conjecture** : une porte qui compare des captures suffit à faire respecter les contrats d'écran.
- **Réfuté par** : deux mesures. (1) Le contrat d'écran Rider a été joué sur les deux arbres, mêmes fixtures, même surface : sur `main` déjà, ISC-F4-26 et ISC-F4-27 échouent. Un golden généré depuis `main` fige donc ces défauts, et la porte reste verte dessus à jamais. (2) Les trois tests ont été remplacés par `void main() {}` dans l'arbre évalué : `flutter test` sort « No tests ran », zéro comparaison, et la porte d'alors affichait vert. Une porte dont la référence ET le harnais sont modifiables par la branche ne mesure rien.
- **Appris** : la valeur d'une porte anti-régression ne vient pas du pixel mais de trois invariants — référence lue hors de la branche, harnais lu hors de la branche, et échec explicite quand la référence est absente (`exit 2`, jamais 0). Corollaire qui change la façon de lire un rouge : la porte nomme un **changement**, pas un défaut. Le partage « ce que le rouge révèle » (F4-25) contre « ce que la référence fige » (F4-26/27) doit être écrit dans le README de l'app, sinon le premier rouge sera lu comme « la porte invente un bug ».
- **Appris** : l'écart rapporté par Papa (SOS / notes / profil absents) n'est pas reproductible en `RIDE_RUNNING` pour le SOS (PASS sur les deux arbres). La branche n'a cassé que la tuile notes. Chercher le reste ailleurs que dans la feuille de course évite de « corriger » un écran intact.
- **Critère maintenant** : ISC-EMU-4 exige la porte **plus** ses trois invariants ; ISC-EMU-3 (verdict e2e global) reste non livré et ne peut pas être coché par cette porte seule.

## Verification

(Aucune entrée — aucun ISC fermé à ce tour.)
- ISC-EMU-4: preuve locale 2026-11-23 (workflow jamais dispatché, aucun jeton Actions) — gate.sh sur base 08cc8cd = VERT 10/10 GATE_RC=0, sur la branche 47a2ee2 = ROUGE 5/10 GATE_RC=1 (plus gros écart 27,33 pour cent), PNG falsifié = ROUGE, harnais void main = ROUGE avec 10 tests rejoués, accept = RC 0 sans toucher la référence, base origin/main sans goldens = RC 2. bash -n et py_compile et parse YAML OK. [@c6a782e]

## Remaining Work

- [ ] Confirmer avec Papa l'emplacement de la porte CI golden-diff (`galass-emu-actions-pub` vs `galass-ci`) avant d'écrire le moindre workflow YAML.
- [ ] Fixer le contrat exact du repère invisible stable avec les ISA Rider (ISC-F4-22) et Driver (ISC-F5-14) avant d'écrire ISC-EMU-1 dans le harnais.
- [ ] Construire l'infra `test/goldens/` dans Rider et Driver (worktree séparé sur `main`, données/appareil fixes) — dépendance bloquante pour F2/F4, non construite ce tour.
- [ ] Une fois F1-F4 livrés : Bridge final vers Rider (`Bridge-B7`, 20261006-galass-rider-multistop) et Driver (`Bridge-B7`, 20261006-galass-driver-multistop).
- [ ] **Décision Papa sur le leg goldens** : `test/goldens/rider/` (10 PNG + harnais + fixtures) doit être fusionné sur la branche de référence pour que `--base origin/main` passe du `exit 2` (bootstrap) au vert. Ce run ne pousse ni ne fusionne rien — un commit local existe côté Rider, rien n'est parti sur le remote.
- [ ] **Dispatch réel du workflow `golden-diff.yml`** : jamais exécuté (aucun jeton Actions dans cet environnement). Tout est vérifié localement (`bash -n`, `python3 -m py_compile board.py`, YAML parsé) mais l'ISC-EMU-4 est cochée sur la preuve locale, pas sur un run CI vert.
- [ ] Blocage résiduel — ISC-EMU-4, libellé 2 du texte** : le libellé d'origine dit encore « Bloqué : emplacement non confirmé ». L'emplacement est tranché et la porte existe, mais le libellé n'a pas été réécrit (le garde d'écriture ISA refuse les caractères du texte d'origine) — à reformuler au prochain passage.
- [ ] Bloqué — ISC-EMU-1 reste bloqué** : le contrat de clé/sémantique du repère stable n'est pas écrit, et il dépend d'ISC-F4-22 (code produit Rider, interdit par la garde de ce run) plus de l'équivalent Driver. Ne pas implémenter à l'aveugle.
- [ ] Non livré — ISC-EMU-3 non livré** : aucun e2e ce run (interdit par le cadrage), donc pas de verdict global à collecter.
- [ ] **Driver** : l'emplacement `test/goldens/driver/` n'existe pas ; la porte sort en `exit 2` avec « aucun golden » — c'est documenté, pas un bug.
- [ ] **Une fois F1-F4 livrés** : Bridge final vers Rider (`Bridge-B7`, 20261006-galass-rider-multistop) et Driver (`Bridge-B7`, 20261006-galass-driver-multistop).

