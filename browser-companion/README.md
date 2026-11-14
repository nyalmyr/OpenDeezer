# OpenDeezer Compagnon — Chrome / Edge

Extension facultative. Aucun service externe, aucune télémétrie, aucun mot de passe collecté. Le cookie ARL est une session sensible : utilisez uniquement votre propre serveur. Une page web seule ne peut pas lire les cookies d'un autre site.

## Installation

1. Décompresser le dossier `browser-companion`.
2. Ouvrir `chrome://extensions` ou `edge://extensions`, activer le mode développeur, puis **Charger l'extension non empaquetée** et sélectionner ce dossier.
3. Épingler OpenDeezer Compagnon à la barre d'outils.

## Ajouter un membre

1. Dans Music Assistant, ouvrir `/opendeezer-family/`, se connecter comme administrateur, indiquer le prénom puis **Connecter avec le compagnon navigateur**.
2. Dans l'extension, **Utiliser la demande de cette page**. Vérifier le prénom et l'adresse de destination.
3. **Ouvrir Deezer et se connecter**. Si un autre membre est déjà connecté, le déconnecter de Deezer puis connecter le compte voulu.
4. Rouvrir l'extension, **Connecter ce compte au serveur**. Autoriser uniquement l'adresse de son serveur.
5. Revenir à Musique en famille : le compte est ajouté après vérification. Le ticket expire après 10 minutes et n'est utilisable qu'une fois.

L'ARL n'est pas conservé par l'extension ; il est transmis à la passerelle privée du serveur. Sur un réseau local HTTP, ce transfert n'est pas chiffré ; utiliser HTTPS lorsque le serveur le propose. Les cookies des autres sites ne sont pas accessibles à cette extension. Les permissions de destination sont demandées au clic et retirées après le transfert réussi. Le mode incognito et Firefox ne sont pas validés par cette version.

Pour retirer un membre, utiliser **Retirer ce compte** dans Musique en famille. Pour déconnecter aussi les autres appareils Deezer, utiliser les réglages de sécurité Deezer.
