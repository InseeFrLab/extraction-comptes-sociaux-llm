"""Conversion des sorties brutes des moteurs en grilles, puis en CSV.

Le point d'entrée du pipeline est [`json_to_csv.py`](../json_to_csv.py), qui porte la CLI
et réexporte ce qu'utilisent les autres scripts. Ce package en porte le code, découpé
selon la chaîne de traitement — chaque module ne dépend que des précédents :

| Module | Rôle |
|---|---|
| `grid` | Mise en forme des matrices de chaînes : largeur, en-têtes, lignes empilées. Ne connaît ni HTML, ni moteur, ni S3. |
| `html_tables` | Parseur HTML tolérant : un fragment → une matrice par `<table>`, fusions comprises. |
| `chandra` | Découpage d'une page chandra en blocs de mise en page, et recollage des tableaux qu'un intertitre a coupés. |
| `extractors` | Un lecteur par format de sortie de moteur (marker, chandra, opendataloader). |
| `pipeline` | Lecture S3 → conversion → écriture d'un CSV par tableau, et compte rendu. |
| `cli` | Analyse des arguments de `json_to_csv.py`. |

**Les choix de conversion et les mesures qui les fondent sont dans
[README.md](../README.md)**, section « Étape 2 » : traitement des fusions, tolérance du
parseur, découpage des lignes empilées, placement des sous-lignes d'en-tête, recollage des
blocs chandra.
"""
