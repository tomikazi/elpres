# El Presidente

Web-based multiplayer El Presidente card game. 3–7 players, standard 52-card deck.

## Build

```bash
./build
```

## Run

```bash
./run
```

Then open http://localhost:8765/elpres/

## Docker

```bash
docker build -t elpres .
docker run -p 8765:8765 -v elpres-data:/elpres elpres
```

Game state is stored in the `/elpres` volume. Open http://localhost:8765/elpres/
