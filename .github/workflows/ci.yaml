name: Build & Push – extraction-comptes-sociaux-llm

# Déclencheurs : à chaque push sur main ou sur un tag, et possibilité de déclencher manuellement
on:
  push:
    branches:
      - main
    tags:
      - '*'
  workflow_dispatch:

jobs:
  build-and-push:
    runs-on: ubuntu-latest

    # Matrice pour chaque sous-dossier (chaque service)
    strategy:
      matrix:
        include:
          - directory: api_centrale
            suffix: api_centrale
          - directory: api_marker
            suffix: api_marker
          - directory: marker_proxy
            suffix: marker_proxy

    steps:
      # 1) On récupère tout le code
      - name: Checkout du code
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      # 2) (Optionnel) Libérer de l’espace si on build plusieurs images lourdes
      - name: Faire de la place
        run: |
          sudo rm -rf /usr/share/dotnet /opt/ghc /usr/local/share/boost "$AGENT_TOOLSDIRECTORY"
          docker rmi -f $(docker images -aq) || true
        shell: bash

      # 3) Installer QEMU (pour build multi-arch si nécessaire)
      - name: Set up QEMU
        uses: docker/setup-qemu-action@v3

      # 4) Installer Docker Buildx
      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      # 5) Se connecter à Docker Hub (ou autre registre)
      - name: Login Docker Hub
        uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}

      # 6) Build & Push de l’image pour chaque service
      - name: Build & Push ${{ matrix.suffix }}
        id: build_and_push
        uses: docker/build-push-action@v6
        with:
          context: ${{ matrix.directory }}
          push: true
          tags: |
            inseefrlab/extraction-comptes-sociaux-llm:${{ matrix.suffix }}-latest
            inseefrlab/extraction-comptes-sociaux-llm:${{ matrix.suffix }}-${{ github.sha }}
          build-args: |
            GIT_COMMIT_MESSAGE=${{ github.event.head_commit.message }}
            GIT_VERSION_HASH=${{ github.sha }}

      # 7) Afficher en sortie le digest (facultatif)
      - name: Afficher le digest
        run: |
          echo "Service       : ${{ matrix.suffix }}"
          echo "Image tag     : inseefrlab/extraction-comptes-sociaux-llm:${{ matrix.suffix }}-latest"
          echo "Image digest  : ${{ steps.build_and_push.outputs.digest }}"
