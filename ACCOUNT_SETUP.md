# Ajouter un compte familial — OpenDeezer Home 0.4.0

## Connexion sans extension

Le parcours actif utilise le formulaire natif Music Assistant décrit ci-dessous. Aucune extension de navigateur à installer. Une simple fenêtre de connexion Deezer ouverte par Home Assistant ne peut pas récupérer son cookie : les deux sites sont isolés par le navigateur. Aucune fenêtre email/mot de passe prétendant contourner cette limite n'est proposée.

Les applications natives OpenDeezer utilisent un navigateur embarqué pour cette étape ; ce mécanisme n'est pas raccordé automatiquement à cette intégration. L'ancien compagnon expérimental reste dans le dépôt pour référence, mais n'est plus proposé par l'interface courante.

## Formulaire natif
Dans Music Assistant, connecté avec un utilisateur autorisé à gérer les fournisseurs :

1. Ouvrir **Paramètres → Music sources → Add a music source**.
2. Chercher et choisir **OpenDeezer Home** (pas le fournisseur officiel « Deezer »).
3. Renseigner le **prénom / nom du membre**.
4. Sur un ordinateur, ouvrir deezer.com avec le compte de cette personne. Dans les outils du navigateur (F12), ouvrir **Application → Cookies** dans Chrome/Edge ou **Stockage → Cookies** dans Firefox. Sélectionner le domaine Deezer puis copier la **valeur** du cookie `arl`.
5. Coller cette valeur uniquement dans **Cookie de connexion Deezer (ARL)**, puis cliquer **Finish**.
6. Attendre l'import des playlists. Elles portent le prénom du membre. Répéter pour chaque compte distinct.

Le bouton d'aide à côté de « Connecter un compte Deezer familial » rappelle ces étapes. Il n'existe pas de connexion OAuth officielle dans cette extension ; la récupération automatique par une simple popup web n'est pas implémentée.

Le cookie est une clé d'accès au compte : ne jamais l'envoyer dans un chat ni le publier. La connexion est testée avant enregistrement. L'ARL reste dans le stockage privé de la passerelle ; Music Assistant conserve seulement la clé du profil dans ses données chiffrées. L'instance Music Assistant et son réseau doivent être de confiance.

## Ce qui est automatique

- Validation de la connexion et génération d'une clé par profil.
- Sauvegarde privée atomique et activation sans redémarrage de la passerelle.
- Réutilisation du profil si le même compte est présenté à nouveau ; refus de doublon dans les fournisseurs MA déjà chargés.
- Refus d'un prénom déjà utilisé pour un autre compte et messages d'erreur en français.
- Isolation des bibliothèques, préfixe prénom des playlists et conservation des profils existants.

## Limites

La nouvelle carte **Musique en famille** affiche les comptes ; la reconnexion se fait dans les paramètres du fournisseur natif MA. Les routes administratives de reconnexion et révocation restent disponibles. Le retrait supprime son accès dans la passerelle et ses préférences/mélanges locaux, puis le fournisseur MA ; il ne supprime pas le compte Deezer. Un flux déjà ouvert peut finir. Retirer uniquement le fournisseur par les paramètres génériques MA ne détruit pas le profil de la passerelle. Un compte désactivé doit être réactivé plutôt qu'ajouté une deuxième fois.

Le prénom sélectionne une bibliothèque ; ce n'est pas une reconnaissance biométrique de la voix. Les commandes vocales peuvent préciser le prénom dans le nom de la playlist. La lecture simultanée de plusieurs comptes réels reste à valider avec ces comptes.

## Configuration administrateur de l'installation

La passerelle doit lire un secret aléatoire d'au moins 32 caractères via `OPENDEEZER_ADMIN_TOKEN_FILE`, monté en lecture seule. Music Assistant lit le même fichier avec cette variable. Ce secret n'est jamais demandé dans le formulaire et ne permet pas de lire un flux musical à la place d'une clé de profil.

La passerelle utilise `OPENDEEZER_MANAGED_PROFILES_FILE` pour un fichier JSON dans un répertoire privé inscriptible par son utilisateur. Fichiers0600, répertoire0700. Les profils statiques existants dans config.json restent compatibles. L'ajout est désactivé si la variable du secret n'est pas définie.

MA contacte par défaut `http://127.0.0.1:8731/admin/enroll`. Une autre adresse peut être définie par l'administrateur dans `OPENDEEZER_ENROLL_URL`, jamais par le formulaire. Aucun nouveau port public n'est nécessaire. Utiliser HTTPS si le trafic sort d'un hôte ou d'un réseau de confiance.
