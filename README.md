# NumTube : YouTube sur NumWorks

NumTube met YouTube sur une calculatrice NumWorks reliée en USB à un ordinateur
(Mac ou Windows). La calculatrice cherche et affiche les vidéos ; l'ordinateur
fait passerelle : il interroge YouTube avec yt-dlp, décode la vidéo avec ffmpeg
et l'envoie image par image. La NumWorks n'a pas de son, donc les sous-titres
YouTube (français en priorité) s'affichent sous l'image.

- Vidéo 320×180 à environ 15 images/s, compressée en blocs de 4×4 pixels
- Recherche, pause, avance et retour rapides depuis le clavier de la calculatrice
- Une appli (Mac et Windows) qui installe le firmware en un clic et fait passerelle

> **Nouveau (v2)** : NumTube fait partie de **[NumSocial](https://github.com/derim00/numsocial)**.
> Le même firmware contient aussi **[NumChat](https://github.com/derim00/numchat)**
> (Snapchat sur la calculatrice) et **[NumScreen](https://github.com/derim00/numscreen)** (l'écran de l'ordinateur sur la
> calculatrice, avec la souris et le clavier), et l'appli s'appelle désormais
> **NumSocial** : au moment d'installer, tu coches les applis que tu veux.

## Sommaire

1. [Pourquoi un patch et pas un firmware](#pourquoi-un-patch-et-pas-un-firmware)
2. [Ce qu'il faut](#ce-quil-faut)
3. [Installation sans rien compiler (Windows ou Mac)](#installation-sans-rien-compiler-windows-ou-mac)
4. [Installation en compilant sur ton Mac](#installation-en-compilant-sur-ton-mac)
5. [Installer sur la calculatrice](#installer-sur-la-calculatrice)
6. [Utilisation](#utilisation)
7. [Comment ça marche](#comment-ça-marche)
8. [Tests](#tests)
9. [Avertissements](#avertissements)

## Pourquoi un patch et pas un firmware

Depuis juillet 2021, le code d'Epsilon (le logiciel de la NumWorks) n'est plus
sous licence libre : NumWorks n'autorise plus sa redistribution. Ce dépôt ne
contient donc **aucun code ni aucun firmware de NumWorks**. Il contient :

| Dossier | Contenu |
| --- | --- |
| `patches/numsocial-epsilon.patch` | les modifications à appliquer à Epsilon (lien USB de Termia, NumTube, NumChat, NumScreen, choix des applis) |
| `EPSILON_COMMIT` | la version exacte d'Epsilon sur laquelle le patch s'applique |
| `scripts/build-firmware.sh` | télécharge Epsilon chez NumWorks, applique le patch, compile |
| `app/` | l'appli NumSocial : installeur et passerelle (page dans le navigateur) |
| `gateway/` | la passerelle seule, en ligne de commande (`numtube_gateway.py` pour YouTube, `numchat_*.py` pour Snapchat, `numscreen_*.py` pour le partage d'écran) |
| `tests/` | tests du codec vidéo et fausses calculatrices pour tester la passerelle |

Chacun récupère Epsilon depuis [le dépôt officiel de NumWorks](https://github.com/numworks/epsilon)
et compile le firmware sur sa propre machine (ou dans sa copie privée sur
GitHub), comme le décrit
[la documentation de NumWorks](https://github.com/numworks/epsilon/blob/master/epsilon/docs/build/README.md).
Le firmware compilé et l'appli qui l'embarque sont pour ton usage : ne les
partage pas, partage ce dépôt.

## Ce qu'il faut

- Un Mac (testé avec une calculatrice) ou un PC Windows (compilé et vérifié par
  GitHub, pas encore essayé avec une calculatrice)
- Une NumWorks **N0120** ou **N0115** avec la version officielle **26.4.0**
  (testé ; d'autres versions peuvent afficher « OFFICIAL UPGRADE REQUIRED »)
- Un câble USB qui transfère les données

## Installation sans rien compiler (Windows ou Mac)

GitHub peut tout compiler pour toi, dans **ta propre copie privée** :

1. En haut de cette page : **Use this template** > **Create a new repository**,
   coche **Private** (obligatoire : la compilation refuse de tourner dans un
   dépôt public, pour que le firmware ne soit pas téléchargeable par tous).
2. Dans ta copie : onglet **Actions** > **Build NumSocial** > **Run workflow**.
3. Environ 15 minutes plus tard, en bas de la page de la compilation, télécharge
   **NumSocial-windows** ou **NumSocial-mac**, décompresse.
4. Windows : lance `NumSocial\NumSocial.exe` (SmartScreen : « Informations
   complémentaires » > « Exécuter quand même »). Mac : clic droit sur
   `NumSocial.app` > Ouvrir.

La calculatrice déclare le pilote WinUSB : sous Windows, aucun pilote n'est à
installer.

## Installation en compilant sur ton Mac

Il faut `git`, `make`, `python3` et le kit de compilation ARM de NumWorks.

```bash
git clone https://github.com/derim00/numtube.git && cd numtube
./scripts/build-firmware.sh
```

Le script télécharge Epsilon, applique le patch et compile 4 images
(N0120/N0115, slots A/B). S'il manque le compilateur ARM, il affiche la commande
du kit officiel de NumWorks à lancer, puis relance-le.

Ensuite, fabrique l'appli :

```bash
.venv/bin/python3 app/build.py
open app/dist/NumSocial.app
```

## Installer sur la calculatrice

Une page s'ouvre dans le navigateur (`http://127.0.0.1:8737`) :

1. Branche la calculatrice : elle affiche « Calculatrice connectée »
   (sinon reset au dos, puis rebranche).
2. L'appli lit la version officielle de la calculatrice et l'inscrit dans le firmware.
3. Coche **NumTube** (et **NumChat**, **NumScreen** si tu les veux aussi).
4. Clique **Installer sur la calculatrice** (environ 1 minute).

Le firmware officiel n'est jamais effacé : NumSocial va dans l'autre emplacement
de la mémoire, et un reset au dos ramène le firmware officiel.

## Utilisation

Laisse l'appli NumSocial ouverte sur l'ordinateur, puis ouvre **NumTube** sur la
calculatrice (en bas de l'écran d'accueil) :

| Écran | Touches |
| --- | --- |
| Recherche | lettres (alpha verrouillé), `⌫` efface, `EXE` lance |
| Résultats | `▲` `▼` choisir, `OK` lire, retour pour revenir à la recherche |
| Lecture | `OK` pause, `◀` `▶` ±10 s, `▲` `▼` ±1 min, retour pour arrêter |

Sans l'appli, la passerelle seule se lance avec `./numsocial`.

## Comment ça marche

- **USB** : la calculatrice se présente comme un périphérique USB à interface
  propriétaire (VID 0x0483, PID 0xA292), que la passerelle pilote directement
  avec libusb (pas de port série). Le transport et le format des trames (`TMIA` :
  magic, version, type, longueur, données, CRC-16) viennent de
  [Termia](https://github.com/Matinthereal/termia).
- **Vidéo** : 3 600 blocs de 4×4 pixels par image, envoyés en couleur unie ou
  en 2 couleurs + 2 intermédiaires (à la manière de DXT1), et seulement s'ils
  ont changé. La calculatrice décode directement vers l'écran, sans tampon d'image.
- **Débit** : chaque image est acquittée ; au plus 2 images en vol, sinon la
  passerelle saute des images au lieu de prendre du retard.
- **Version** : le noyau officiel n'accepte qu'un firmware qui annonce la même
  version que lui. L'installeur écrit cette version dans l'image avant de flasher.
- **Choix des applis** : NumTube, NumChat et NumScreen sont toujours compilés ensemble.
  L'installeur écrit dans l'image un masque (repéré par la chaîne
  `NUMAPPS-HIDDEN1`) qui dit à l'écran d'accueil lesquelles cacher.

Corrections apportées à la pile USB de Termia, incluses dans le patch :
une requête USB adressée à un canal lisait hors d'un tableau à 2 cases (plantage
ou lien perdu), et la fin des envois n'était jamais vue pendant une réception
continue (accusés de réception bloqués).

## Tests

```bash
cd tests
../.venv/bin/python3 codec_test.py              # décodeur C++ de la calculatrice == encodeur, au bit près
../.venv/bin/python3 fake_calculator.py "numworks"   # fausse calculatrice NumTube contre la vraie passerelle
../.venv/bin/python3 fake_numchat.py                 # fausse calculatrice NumChat (messagerie de démo)
../.venv/bin/python3 fake_numscreen.py               # fausse calculatrice NumScreen (image de test, rien n'est cliqué)
```

## Avertissements

- Projet non officiel, sans lien avec NumWorks, YouTube ou Snapchat. NumWorks est
  une marque déposée.
- Télécharger des vidéos avec yt-dlp va à l'encontre des conditions
  d'utilisation de YouTube.
- Un firmware non officiel ne peut pas passer en mode examen.

## Licence

Le code de ce dépôt est sous licence
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.fr),
comme Termia dont il reprend le lien USB. Voir [LICENSE](LICENSE).
