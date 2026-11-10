# NumTube : YouTube sur NumWorks

NumTube met YouTube sur une calculatrice NumWorks reliée en USB à un Mac.
La calculatrice cherche et affiche les vidéos ; l'ordinateur fait passerelle :
il interroge YouTube avec yt-dlp, décode la vidéo avec ffmpeg et l'envoie
image par image. La NumWorks n'a pas de son, donc les sous-titres YouTube
(français en priorité) s'affichent sous l'image.

- Vidéo 320×180 à environ 15 images/s, compressée en blocs de 4×4 pixels
- Recherche, pause, avance et retour rapides depuis le clavier de la calculatrice
- Une appli Mac qui installe le firmware en un clic et fait tourner la passerelle

## Pourquoi un patch et pas un firmware

Depuis juillet 2021, le code d'Epsilon (le logiciel de la NumWorks) n'est plus
sous licence libre : NumWorks n'autorise plus sa redistribution. Ce dépôt ne
contient donc **aucun code ni aucun firmware de NumWorks**. Il contient :

| Dossier | Contenu |
| --- | --- |
| `patches/numtube-epsilon.patch` | les modifications à appliquer à Epsilon (lien USB de Termia + appli NumTube) |
| `EPSILON_COMMIT` | la version exacte d'Epsilon sur laquelle le patch s'applique |
| `scripts/build-firmware.sh` | télécharge Epsilon chez NumWorks, applique le patch, compile |
| `app/` | l'appli NumTube : installeur et passerelle (page dans le navigateur) |
| `gateway/` | la passerelle YouTube seule, en ligne de commande |
| `tests/` | tests du codec vidéo et fausse calculatrice pour tester la passerelle |

Chacun récupère Epsilon depuis [le dépôt officiel de NumWorks](https://github.com/numworks/epsilon)
et compile le firmware sur sa propre machine, comme le décrit
[la documentation de NumWorks](https://github.com/numworks/epsilon/blob/master/epsilon/docs/build/README.md).
Le firmware compilé et l'appli qui l'embarque sont pour ton usage : ne les
partage pas, partage ce dépôt.

## Ce qu'il faut

- Un Mac (testé) ou un PC Windows (compilé par GitHub, pas testé avec une calculatrice)
- Une NumWorks **N0120** ou **N0115** avec la version officielle **26.4.0**
  (testé ; d'autres versions peuvent afficher « OFFICIAL UPGRADE REQUIRED »)
- Un câble USB qui transfère les données
- `git`, `make`, `python3` et le kit de compilation ARM de NumWorks

## Installation sans rien compiler (Windows ou Mac)

GitHub peut tout compiler pour toi, dans **ta propre copie privée** :

1. En haut de cette page : **Use this template** > **Create a new repository**,
   coche **Private** (obligatoire : la compilation refuse de tourner dans un
   dépôt public, pour que le firmware ne soit pas téléchargeable par tous).
2. Dans ta copie : onglet **Actions** > **Build NumTube** > **Run workflow**.
3. Environ 15 minutes plus tard, en bas de la page de la compilation, télécharge
   **NumTube-windows** ou **NumTube-mac**, décompresse.
4. Windows : lance `NumTube\NumTube.exe` (SmartScreen : « Informations
   complémentaires » > « Exécuter quand même »). Mac : clic droit sur
   `NumTube.app` > Ouvrir.

La version Windows n'a été vérifiée qu'en démarrage automatique sur les
machines de GitHub, pas avec une vraie calculatrice. La calculatrice déclare
le pilote WinUSB, donc aucun pilote ne devrait être à installer.

## Installation en compilant sur ton Mac

```bash
git clone <ce dépôt> numtube && cd numtube
./scripts/build-firmware.sh
```

Le script télécharge Epsilon, applique le patch et compile 4 images
(N0120/N0115, slots A/B). S'il manque le compilateur ARM, il affiche la commande
du kit officiel de NumWorks à lancer, puis relance-le.

Ensuite, fabrique l'appli :

```bash
.venv/bin/python3 app/build.py
open app/dist/NumTube.app
```

Une page s'ouvre dans le navigateur :

1. Branche la calculatrice : elle affiche « Calculatrice connectée »
   (sinon reset au dos, puis rebranche).
2. L'appli lit la version officielle de la calculatrice et l'inscrit dans le firmware.
3. Clique **Installer sur la calculatrice** (environ 1 minute).

Le firmware officiel n'est jamais effacé : NumTube va dans l'autre emplacement
de la mémoire, et un reset au dos ramène le firmware officiel.

## Utilisation

Laisse l'appli NumTube ouverte sur l'ordinateur, puis sur la calculatrice :

| Écran | Touches |
| --- | --- |
| Recherche | lettres (alpha verrouillé), `⌫` efface, `EXE` lance |
| Résultats | `▲` `▼` choisir, `OK` lire, retour pour revenir à la recherche |
| Lecture | `OK` pause, `◀` `▶` ±10 s, `▲` `▼` ±1 min, retour pour arrêter |

Sans l'appli, la passerelle seule se lance avec `./numtube`.

## Comment ça marche

- **USB** : la calculatrice se présente comme un périphérique USB à interface
  propriétaire, que la passerelle pilote directement avec libusb (pas de port
  série). Le transport et le format des trames (`TMIA` : magic, version, type,
  longueur, données, CRC-16) viennent de [Termia](https://github.com/Matinthereal/termia).
- **Vidéo** : 3 600 blocs de 4×4 pixels par image, envoyés en couleur unie ou
  en 2 couleurs + 2 intermédiaires (à la manière de DXT1), et seulement s'ils
  ont changé. La calculatrice décode directement vers l'écran, sans tampon d'image.
- **Débit** : chaque image est acquittée ; au plus 2 images en vol, sinon la
  passerelle saute des images au lieu de prendre du retard.
- **Version** : le noyau officiel n'accepte qu'un firmware qui annonce la même
  version que lui. L'installeur écrit cette version dans l'image avant de flasher.

Corrections apportées à la pile USB de Termia, incluses dans le patch :
une requête USB adressée à un canal lisait hors d'un tableau à 2 cases (plantage
ou lien perdu), et la fin des envois n'était jamais vue pendant une réception
continue (accusés de réception bloqués).

## Tests

```bash
cd tests
../.venv/bin/python3 codec_test.py              # décodeur C++ de la calculatrice == encodeur, au bit près
../.venv/bin/python3 fake_calculator.py "numworks"   # fausse calculatrice contre la vraie passerelle
```

## Avertissements

- Projet non officiel, sans lien avec NumWorks. NumWorks est une marque déposée.
- Télécharger des vidéos avec yt-dlp va à l'encontre des conditions
  d'utilisation de YouTube.
- Un firmware non officiel ne peut pas passer en mode examen.

## Licence

Le code de ce dépôt est sous licence
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.fr),
comme Termia dont il reprend le lien USB. Voir [LICENSE](LICENSE).
