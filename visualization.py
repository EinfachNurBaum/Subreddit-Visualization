from argparse import ArgumentParser
from json import dumps, dump, load as json_load, JSONDecodeError
from functools import partial, lru_cache
from redis import Redis, ConnectionError
import pickle
from pyvis.network import Network
from time import sleep, time
from logging import getLogger, basicConfig as loggingConfig, INFO as LOG_INFO
import http.server
import socketserver
import json
from webbrowser import open as webb_open
from os import path, makedirs
from threading import Thread
from typing import Dict, Set, List, Tuple, Optional, Any

# Configure logging
loggingConfig(
    level=LOG_INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='visualization.log'
)
logger = getLogger(__name__)


class GraphHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP-Handler für die Visualisierung. Unterdrückt Standard-Logging."""

    def log_message(self, formatt: str, *args: Any) -> None:
        # Standardmäßiges Logging wird unterdrückt, um die Konsole übersichtlich zu halten
        pass


# Benutzerdefinierter ThreadingTCPServer, um mehrere Anfragen gleichzeitig zu verarbeiten
class ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


# Kombinierter Handler für statische Dateien und Export-Anfragen
class ExportStaticHandler(http.server.SimpleHTTPRequestHandler):
    """Kombinierter Handler für statische Dateien und Export-Anfragen"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.directory = kwargs.get('directory', 'web')
        super().__init__(*args, **kwargs)

    def do_POST(self) -> None:
        """Verarbeitet POST-Anfragen für Export und Merge-Operationen"""
        if self.path == '/export':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)

            try:
                data = json.loads(post_data.decode('utf-8'))

                # Zugriff auf Visualizer-Instanz
                visualizer = self.server.visualizer

                # Export durchführen
                file_path = visualizer.export_clusters_as_image(
                    data['clusters'],
                    data['resolution'],
                    data['layout']
                )

                # Antwort senden
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()

                if file_path:
                    response = {
                        'success': True,
                        'file': file_path
                    }
                else:
                    response = {
                        'success': False,
                        'error': 'Export fehlgeschlagen'
                    }

                self.wfile.write(json.dumps(response).encode('utf-8'))
            except Exception as e:
                logger.error(f"Fehler bei Export-Anfrage: {e}", exc_info=True)
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                response = {
                    'success': False,
                    'error': str(e)
                }
                self.wfile.write(json.dumps(response).encode('utf-8'))
        elif self.path == '/merge':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)

            try:
                data = json.loads(post_data.decode('utf-8'))
                visualizer = self.server.visualizer
                merged_file = visualizer.merge_selected_clusters(data['clusters'])

                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()

                if merged_file:
                    # Extrahiere die Knotenanzahl aus dem Dateinamen
                    node_count = 0
                    if "_nodes_" in merged_file:
                        try:
                            node_count = int(merged_file.split("_nodes_")[0].split("_")[-1])
                        except (ValueError, IndexError):
                            pass

                    response = {
                        'success': True,
                        'file': merged_file,
                        'nodeCount': node_count
                    }
                else:
                    response = {
                        'success': False,
                        'error': 'Merging failed'
                    }

                self.wfile.write(json.dumps(response).encode('utf-8'))
            except Exception as e:
                logger.error(f"Error in merge request: {e}", exc_info=True)
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                response = {
                    'success': False,
                    'error': str(e)
                }
                self.wfile.write(json.dumps(response).encode('utf-8'))
        else:
            super().do_GET()

    def log_message(self, formatt: str, *args: Any) -> None:
        # Standardmäßiges Logging wird unterdrückt
        pass


class RedditGraphVisualizer:
    """
    Visualisiert Reddit-Netzwerkdaten mithilfe von PyVis und stellt
    diese über einen eingebetteten Webserver dar.
    """

    def __init__(self, use_server: bool = False, batch_size: int = 30) -> None:
        """
        Initialisiert den Visualisierer:
         - Verbindet zu Redis (falls gewünscht).
         - Startet einen lokalen HTTP-Server.
         - Konfiguriert das PyVis-Netzwerk.
         - Initialisiert interne Datenstrukturen.

        Args:
            use_server: Ob eine Verbindung zu Redis hergestellt werden soll
            batch_size: Anzahl der Datensätze, die pro Batch verarbeitet werden
        """
        self.use_server: bool = use_server
        self.queue_name: str = "reddit_graph_queue"

        # Verbindung zu Redis herstellen
        self.redis_client: Optional[Redis] = None
        if self.use_server:
            self.setup_redis_connection()
        else:
            logger.info("Server connection not enabled. Running in JSON-only mode.")

        # Konfiguration für den Webserver
        self.port: int = 8000  # Standard-Port
        self.web_dir: str = "web"
        self.html_filename: str = "graph.html"  # Datei im Web-Verzeichnis

        # Erstelle das PyVis-Netzwerk (hauptsächlich für Legacy-Support)
        self.net: Network = self.create_network_with_options()

        # Sets zur Vermeidung von Duplikaten
        self.added_nodes: Set[str] = set()
        self.added_edges: Set[Tuple[str, str]] = set()

        # Neue Clustering-Funktionalität
        self.clusters: Dict[str, Dict[str, Any]] = {}  # Dictionary to store clusters by search term
        self.active_clusters: Set[str] = set()  # Currently displayed clusters
        self.clusters_dir: str = path.join(self.web_dir, "clusters")
        makedirs(self.clusters_dir, exist_ok=True)

        # Export-Verzeichnis erstellen
        self.exports_dir: str = path.join(self.web_dir, "exports")
        makedirs(self.exports_dir, exist_ok=True)

        # Starte den HTTP-Server
        self.start_http_server()

        self.batch_size: int = batch_size
        self.update_buffer: List[Dict[str, Any]] = []

        self.last_successful_state: Optional[Dict[str, Any]] = None

        self.MIN_SIZE: int = 10
        self.MAX_SIZE: int = 100
        self.MAX_WEIGHT: int = 1000

        # Erstelle die Interface-HTML-Dateien
        self.create_interface_html()

        logger.info("Visualizer initialized and waiting for data...")

    @staticmethod
    def create_network_with_options() -> Network:
        """
        Erstellt ein neues Netzwerk mit Standardoptionen für die Visualisierung.

        Returns:
            Network: Konfiguriertes PyVis-Netzwerkobjekt
        """
        net = Network(
            height="100vh",  # Volle Bildschirmhöhe
            width="100%",  # Volle Bildschirmbreite
            bgcolor="#222222",  # Dunkler Hintergrund
            font_color="white"  # Weiße Schrift
        )

        # Standard-Konfiguration für das Netzwerk
        options = {
            "configure": {
                "enabled": False
            },
            "physics": {
                "enabled": True,
                "solver": "forceAtlas2Based",  # Barnes-Hut eignet sich in der Regel besser für große Netzwerke
                "forceAtlas2Based": {
                    "gravitationalConstant": -50,  # Niedrigere negative Kraft für sanftere Abstoßung
                    "centralGravity": 0.01, # Sehr schwache Zentralanziehung, damit Cluster nicht zu stark zusammenfallen
                    "springLength": 150,  # Etwas längere Federn für mehr Abstand zwischen den Nodes
                    "springConstant": 0.08,  # Etwas höhere Federkonstante für stabilere Verbindungen
                    "damping": 0.4,  # Erhöhtes Dämpfungsverhalten, um das Oszillieren zu reduzieren
                    "avoidOverlap": 4  # Leicht erhöhen, um Überschneidungen zu minimieren
                },
                "stabilization": {
                    "enabled": True,
                    "iterations": 1000,
                    "updateInterval": 100
                },
                "minVelocity": 0.1,
                "maxVelocity": 50
            },
            "nodes": {
                "scaling": {
                    "min": 10,
                    "max": 100
                },
                "shadow": {
                    "enabled": True,
                    "size": 15
                },
                "font": {
                    "size": 12,
                    "face": "Arial"
                }
            },
            "edges": {
                "smooth": {
                    "enabled": False,
                    "type": "dynamic"  # Dynamische Kanten können den Fluss des Netzwerks besser betonen
                },
                "color": {
                    "inherit": "from"  # Kantenfarbe von den verbundenen Knoten erben
                },
                "width": 1.5
            },
            "interaction": {
                "hideEdgesOnDrag": True,
                "tooltipDelay": 100
            }
        }

        # Setze die Optionen für das Netzwerk
        net.set_options(dumps(options))
        return net

    def save_state(self) -> None:
        """Speichert den aktuellen Zustand des Graphen in einer JSON-Datei."""
        # Erstelle eine vereinfachte Version der Cluster für die Speicherung
        serializable_clusters = {}
        for search_term, cluster in self.clusters.items():
            serializable_clusters[search_term] = {
                'nodes': list(cluster['nodes']),
                'edges': list(cluster['edges']),
                'node_count': cluster['node_count'],
                'edge_count': cluster['edge_count']
                # Das Network-Objekt wird ausgelassen
            }

        state = {
            'nodes': list(self.added_nodes),
            'edges': list(self.added_edges),
            'clusters': serializable_clusters
        }

        with open('graph_state.json', 'w') as f:
            dump(state, f)
        logger.info("Graph state saved successfully")

    def start_http_server(self) -> None:
        """
        Startet einen HTTP-Server in einem separaten Thread.
        Dieser Server stellt die Graphen-Visualisierung im Webbrowser dar und
        verarbeitet Export-Anfragen.
        """
        try:
            # Überprüfen, ob der Port verfügbar ist
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('localhost', self.port))
            if result == 0:
                logger.warning(f"Port {self.port} is already in use. Trying alternative port.")
                # Finde einen freien Port
                sock.close()
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(('localhost', 0))
                self.port = sock.getsockname()[1]
                logger.info(f"Using alternative port: {self.port}")
            sock.close()

            makedirs(self.web_dir, exist_ok=True)
            makedirs(self.clusters_dir, exist_ok=True)
            makedirs(self.exports_dir, exist_ok=True)

            # Erstelle eine leere graph.html im web-Verzeichnis
            file_path = path.join(self.web_dir, self.html_filename)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write("<html><body>Loading...</body></html>")

            # Kombinierter Handler für statische Dateien und Export-Anfragen
            handler = partial(ExportStaticHandler, directory=self.web_dir)

            self.httpd = ThreadingTCPServer(("", self.port), handler)
            self.httpd.visualizer = self  # Zugriff auf Visualizer-Instanz

            server_thread = Thread(target=self.httpd.serve_forever, daemon=True)
            server_thread.start()

            logger.info(f"Graph visualization server started at http://localhost:{self.port}")
        except Exception as e:
            logger.error(f"Failed to start HTTP server: {e}", exc_info=True)
            raise SystemExit(1)

    def setup_redis_connection(self) -> None:
        """Stellt die Verbindung zu Redis mit Wiederholungsversuchen her."""
        max_retries = 5
        retry_delay = 5  # Sekunden

        for attempt in range(max_retries):
            try:
                self.redis_client = Redis(
                    host='localhost',
                    port=6379,
                    db=0,
                    socket_connect_timeout=5
                )
                self.redis_client.ping()
                logger.info("Successfully connected to Redis")
                return
            except ConnectionError:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Redis connection attempt {attempt + 1} failed. Retrying in {retry_delay} seconds..."
                    )
                    sleep(retry_delay)
                else:
                    logger.error("Could not connect to Redis after multiple attempts")
                    break  # Bei Fehlschlag, einfach weitermachen

    def update_graph(self) -> bool:
        """
        Verarbeitet neue Daten aus der Redis-Queue (nur, wenn der Server genutzt wird),
        validiert diese und aktualisiert den Graphen, wenn nötig.

        Returns:
            bool: True, wenn neue Daten verarbeitet wurden, sonst False.
        """
        if not self.use_server or not self.redis_client:
            return False

        try:
            batch_data: List[Dict[str, Any]] = []
            updates = 0

            # Sammle Batch von Daten aus Redis
            while updates < self.batch_size:
                raw_data = self.redis_client.lpop(self.queue_name)
                if not raw_data:
                    break

                data = pickle.loads(raw_data)
                if isinstance(data, dict):
                    batch_data.append(data)
                    updates += 1
                else:
                    logger.warning(f"Skipping invalid data format: {data}")

            if not batch_data:
                return False

            # Verarbeite den gesamten Batch
            for data in batch_data:
                # Bestimme den Search Term für dieses Subreddit
                search_term = data.get("search_term", "unknown")

                # Füge es zum entsprechenden Cluster hinzu
                self._add_to_cluster(search_term, data)

            # Update die Interface-HTML
            self.update_interface_html()

            return True
        except Exception as e:
            logger.error(f"Error processing Redis data: {e}", exc_info=True)
            return False

    def _add_to_cluster(self, search_term: str, data: Dict[str, Any]) -> None:
        """
        Fügt Daten zu einem bestimmten Cluster hinzu

        Args:
            search_term: Der Suchbegriff (Cluster-Name)
            data: Die Subreddit-Daten, die hinzugefügt werden sollen
        """
        if search_term not in self.clusters:
            # Erstelle neuen Cluster
            self.clusters[search_term] = {
                'network': self.create_network_with_options(),
                'nodes': set(),
                'edges': set(),
                'node_count': 0,
                'edge_count': 0
            }

        cluster = self.clusters[search_term]
        network = cluster['network']
        added_nodes = cluster['nodes']
        added_edges = cluster['edges']

        # Extrahiere den Subreddit-Namen
        subreddit_name = data.get("name")
        if not subreddit_name:
            logger.warning(f"Skipping data item without name: {data}")
            return

        # Füge Hauptknoten hinzu
        if subreddit_name not in added_nodes:
            self._add_node_to_network(
                network,
                subreddit_name,
                data.get("search_term_count", 1),
                data.get("nsfw", False),
                data.get("related_count", 0),
                added_nodes
            )
            cluster['node_count'] += 1

        # Verarbeite verwandte Subreddits
        related_subreddits = data.get("related_subreddits", [])
        for related in related_subreddits:
            # Entferne r/ Prefix falls vorhanden
            related_name = related.replace("r/", "") if related.startswith("r/") else related

            # Füge verwandten Knoten hinzu falls noch nicht vorhanden
            if related_name not in added_nodes:
                network.add_node(
                    related_name,
                    label=related_name,
                    size=self.MIN_SIZE,
                    color="#ff9999"  # Hellrot für verwandte Knoten
                )
                added_nodes.add(related_name)
                cluster['node_count'] += 1

            # Füge Kante hinzu falls noch nicht vorhanden
            edge = tuple(sorted([subreddit_name, related_name]))
            if edge not in added_edges:
                network.add_edge(*edge, color="#ffffff")
                added_edges.add(edge)
                cluster['edge_count'] += 1

        # Speichere den Cluster
        self.save_cluster(search_term, network)

    def _add_node_to_network(
            self,
            network: Network,
            name: str,
            search_term_count: int,
            is_nsfw: bool,
            related_count: int,
            added_nodes: Set[str]
    ) -> None:
        """
        Fügt einen Knoten mit konsistenter Formatierung zum Netzwerk hinzu

        Args:
            network: Das PyVis-Netzwerk, zu dem der Knoten hinzugefügt werden soll
            name: Name des Knotens (Subreddit-Name)
            search_term_count: Anzahl der Vorkommen des Suchbegriffs
            is_nsfw: Ob das Subreddit als NSFW markiert ist
            related_count: Anzahl verwandter Subreddits
            added_nodes: Set der bereits hinzugefügten Knoten
        """
        if name in added_nodes:
            return

        color = "#ff0000" if is_nsfw else "#00ff00"
        weight = min(search_term_count, self.MAX_WEIGHT)
        size = self.MIN_SIZE + (weight / self.MAX_WEIGHT) * (self.MAX_SIZE - self.MIN_SIZE)

        title = (f"Related count: {related_count}\n"
                 f"Search term count: {search_term_count}\n"
                 f"NSFW: {is_nsfw}")

        network.add_node(
            name,
            label=name,
            title=title,
            size=size,
            color=color
        )
        added_nodes.add(name)

    def save_cluster(self, search_term: str, network: Network) -> None:
        """
        Speichert ein Cluster-Netzwerk in einer eigenen HTML-Datei

        Args:
            search_term: Der Suchbegriff (Cluster-Name)
            network: Das zu speichernde Netzwerk
        """
        # Sanitize search term for filename
        safe_name = ''.join(c if c.isalnum() else '_' for c in search_term)
        file_path = path.join(self.clusters_dir, f"cluster_{safe_name}.html")

        try:
            html_content = network.generate_html()
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            logger.info(f"Saved cluster for '{search_term}' with {len(network.nodes)} nodes")
        except Exception as e:
            logger.error(f"Failed to save cluster for '{search_term}'", exc_info=True)

    def create_interface_html(self) -> None:
        """Erstellt die Haupt-Interface-HTML-Datei"""
        html = """
            <!DOCTYPE html>
            <html>
            <head>
            <meta charset="UTF-8"> 
            <title>Reddit Graph Visualization</title>
            <style>
            body {
                font-family: Arial, sans-serif;
                background-color: #222;
                color: #fff;
                margin: 0;
                padding: 0;
            }
            .container {
                display: flex;
                height: 100vh;
            }
            .sidebar {
                width: 250px;
                background-color: #333;
                padding: 20px;
                overflow-y: auto;
            }
            .main-content {
                flex-grow: 1;
                position: relative;
            }
            iframe {
                width: 100%;
                height: 100%;
                border: none;
            }
            h1, h2 {
                margin-top: 0;
            }
            .cluster-list {
                max-height: 50vh;
                overflow-y: auto;
            }
            .cluster-item {
                padding: 8px;
                margin: 4px 0;
                background-color: #444;
                border-radius: 4px;
                cursor: pointer;
            }
            .cluster-item:hover {
                background-color: #555;
            }
            .cluster-item.active {
                background-color: #0066cc;
            }
            .cluster-info {
                font-size: 0.8em;
                color: #aaa;
            }
            .filters {
                margin: 15px 0;
            }
            .export-controls {
                margin: 15px 0;
                padding-top: 15px;
                border-top: 1px solid #555;
            }
            .export-options {
                margin: 10px 0;
            }
            button {
                background-color: #0066cc;
                color: white;
                border: none;
                padding: 8px 12px;
                border-radius: 4px;
                cursor: pointer;
                margin: 5px 0;
            }
            button:hover {
                background-color: #0055aa;
            }
            button:disabled {
                background-color: #555;
                cursor: not-allowed;
            }
            select, .search-box {
                width: 90%;
                padding: 8px;
                margin: 10px 0;
                background-color: #444;
                border: none;
                color: white;
                border-radius: 4px;
            }
            #exportStatus {
                margin-top: 10px;
                padding: 8px;
                background-color: #444;
                border-radius: 4px;
                min-height: 20px;
            }
            #exportStatus a {
                color: #0099ff;
                text-decoration: none;
            }
            #exportStatus a:hover {
                text-decoration: underline;
            }
            .loading {
                display: inline-block;
                width: 20px;
                height: 20px;
                border: 3px solid rgba(255,255,255,.3);
                border-radius: 50%;
                border-top-color: #fff;
                animation: spin 1s ease-in-out infinite;
            }
            @keyframes spin {
                to { transform: rotate(360deg); }
            }
            </style>
            </head>
            <body>
            <div class="container">
            <div class="sidebar">
                <h1>Reddit Graph</h1>
                <div class="filters">
                    <input type="text" id="searchBox" class="search-box" placeholder="Search clusters...">
                    <button id="btnSelectAll">Select All</button>
                    <button id="btnDeselectAll">Deselect All</button>
                    <button id="btnShowSelected">Show Selected</button>
                    <div>
                        <label>Max clusters: </label>
                        <select id="maxClusters">
                            <option value="5">5</option>
                            <option value="10">10</option>
                            <option value="20" selected>20</option>
                            <option value="50">50</option>
                            <option value="100">100</option>
                            <option value="0">All</option>
                        </select>
                    </div>
                </div>

                <div class="export-controls">
                    <h2>Export</h2>
                    <div class="export-options">
                        <select id="exportResolution">
                            <option value="fullhd">Full HD (1920x1080)</option>
                            <option value="4k">4K (3840x2160)</option>
                            <option value="8k">8K (7680x4320)</option>
                            <option value="16k">16K (15360x8640)</option>
                        </select>
                        <select id="exportLayout">
                            <option value="standard">Standard</option>
                            <option value="labels">Mit großen Labels</option>
                        </select>
                        <button id="btnExport">Exportieren</button>
                    </div>
                    <div id="exportStatus"></div>
                </div>

                <h2>Clusters</h2>
                <div id="clusterList" class="cluster-list">
                    <!-- Clusters will be inserted here -->
                </div>
            </div>
            <div class="main-content">
                <iframe id="graphFrame" src="placeholder.html"></iframe>
            </div>
            </div>
            <script>
            // JavaScript for interface interactions
            const clusters = {};
            let activeFrame = 'placeholder.html';
            let isExporting = false;

            function updateClusterDisplay() {
                const searchTerm = document.getElementById('searchBox').value.toLowerCase();
                const clusterList = document.getElementById('clusterList');
                const maxClusters = parseInt(document.getElementById('maxClusters').value);

                let visibleCount = 0;

                Object.keys(clusters).sort().forEach(name => {
                    const div = document.getElementById(`cluster-${name}`);
                    if (!div) return;

                    const matchesSearch = name.toLowerCase().includes(searchTerm);
                    const withinLimit = maxClusters === 0 || visibleCount < maxClusters;

                    if (matchesSearch && withinLimit) {
                        div.style.display = 'block';
                        visibleCount++;
                    } else {
                        div.style.display = 'none';
                    }
                });
            }

            async function showSelectedClusters() {
                const selected = Object.keys(clusters).filter(name => 
                    clusters[name].selected);

                if (selected.length === 0) {
                    alert('Please select at least one cluster');
                    return;
                }

                if (selected.length === 1) {
                    // Show single cluster
                    setActiveFrame(`clusters/cluster_${selected[0]}.html`);
                    document.getElementById('exportStatus').innerHTML = 
                        `Showing single cluster: ${clusters[selected[0]].name} (${clusters[selected[0]].nodeCount} nodes)`;
                } else {
                    // Show merged view
                    document.getElementById('exportStatus').innerHTML = '<div class="loading"></div> Merging clusters...';

                    try {
                        const response = await fetch('/merge', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json'
                            },
                            body: JSON.stringify({
                                clusters: selected
                            })
                        });

                        if (!response.ok) {
                            throw new Error(`Merging failed: ${response.statusText}`);
                        }

                        const result = await response.json();

                        if (result.success) {
                            setActiveFrame(result.file);
                            // Zeige die Anzahl der Knoten im zusammengeführten Graph an
                            document.getElementById('exportStatus').innerHTML = 
                                `Combined view with ${result.nodeCount || 'multiple'} nodes`;
                        } else {
                            document.getElementById('exportStatus').innerHTML = 
                                `Error: ${result.error}`;
                        }
                    } catch (error) {
                        document.getElementById('exportStatus').innerHTML = 
                            `Error: ${error.message}`;
                    }
                }
            }

            function setActiveFrame(src) {
                document.getElementById('graphFrame').src = src;
                activeFrame = src;
            }

            async function exportSelectedClusters() {
                const selected = Object.keys(clusters).filter(name => clusters[name].selected);
                if (selected.length === 0) {
                        alert('Bitte wählen Sie mindestens einen Cluster aus');
                        return;
                    }

                    if (isExporting) {
                        alert('Export läuft bereits, bitte warten...');
                        return;
                    }

                    const resolution = document.getElementById('exportResolution').value;
                    const layout = document.getElementById('exportLayout').value;

                    // Warning for high-res exports with labels
                    if ((resolution === '8k' || resolution === '16k') && 
                        layout === 'labels' && 
                        !confirm('Hohe Auflösung mit Labels kann längere Zeit in Anspruch nehmen. Fortfahren?')) {
                        return;
                    }

                    const btnExport = document.getElementById('btnExport');
                    btnExport.disabled = true;
                    isExporting = true;

                    document.getElementById('exportStatus').innerHTML = '<div class="loading"></div> Export wird vorbereitet...';

                try {
                    const response = await fetch('/export', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            clusters: selected,
                            resolution: resolution,
                            layout: layout
                        })
                    });

                    if (!response.ok) {
                        throw new Error(`Export fehlgeschlagen: ${response.statusText}`);
                    }

                    const result = await response.json();

                    if (result.success) {
                       document.getElementById('exportStatus').innerHTML = 
                           `<a href="${result.file}" download>Exportierte Datei herunterladen</a>`;
                   } else {
                       document.getElementById('exportStatus').innerHTML = 
                           `Fehler beim Export: ${result.error}`;
                   }
               } catch (error) {
                   document.getElementById('exportStatus').innerHTML = 
                       `Fehler: ${error.message}`;
               } finally {
                   btnExport.disabled = false;
                   isExporting = false;
               }
            }

            document.getElementById('btnSelectAll').addEventListener('click', () => {
               Object.keys(clusters).forEach(name => {
                   clusters[name].selected = true;
                   const element = document.getElementById(`cluster-${name}`);
                   if (element) element.classList.add('active');
               });
            });

            document.getElementById('btnDeselectAll').addEventListener('click', () => {
               Object.keys(clusters).forEach(name => {
                   clusters[name].selected = false;
                   const element = document.getElementById(`cluster-${name}`);
                   if (element) element.classList.remove('active');
               });
            });

            document.getElementById('btnShowSelected').addEventListener('click', showSelectedClusters);
            document.getElementById('btnExport').addEventListener('click', exportSelectedClusters);

            document.getElementById('searchBox').addEventListener('input', updateClusterDisplay);
            document.getElementById('maxClusters').addEventListener('change', updateClusterDisplay);

            // This function will be called to update clusters data
            function updateClusters(clusterData) {
               // Implementation will be generated by Python
            }
            </script>
            </body>
            </html>
           """
        # Create a placeholder file
        placeholder_html = """
           <!DOCTYPE html>
           <html>
           <head>
               <style>
                   body {
                   font-family: Arial, sans-serif;
                   background-color: #222;
                   color: #fff;
                   display: flex;
                   justify-content: center;
                   align-items: center;
                   height: 100vh;
                   margin: 0;
               }
               .message {
                   text-align: center;
                   max-width: 600px;
                   padding: 20px;
                   }
               </style>
           </head>
           <body>
               <div class="message">
               <h1>Reddit Graph Visualization</h1>
               <p>Select one or more clusters from the sidebar to visualize the data.</p>
               </div>
           </body>
           </html>
           """

        try:
            with open(path.join(self.web_dir, self.html_filename), 'w', encoding='utf-8') as f:
                f.write(html)
            with open(path.join(self.web_dir, "placeholder.html"), 'w', encoding='utf-8') as f:
                f.write(placeholder_html)
            logger.info("Created interface HTML files")
        except Exception as e:
            logger.error(f"Failed to create interface HTML: {e}", exc_info=True)

    def update_interface_html(self) -> None:
        """Aktualisiert die Cluster-Liste in der Interface-HTML"""
        js_clusters_data: Dict[str, Dict[str, Any]] = {}
        cluster_html = ""

        for search_term, info in self.clusters.items():
            safe_name = ''.join(c if c.isalnum() else '_' for c in search_term)
            js_clusters_data[safe_name] = {
                'name': search_term,
                'nodeCount': info['node_count'],
                'edgeCount': info['edge_count'],
                'selected': False
            }

            cluster_html += f"""
                <div id="cluster-{safe_name}" class="cluster-item" 
                    onclick="(() => {{
                        clusters['{safe_name}'].selected = !clusters['{safe_name}'].selected;
                        this.classList.toggle('active');
                    }})()">
                    <div>{search_term}</div>
                    <div class="cluster-info">
                        {info['node_count']} nodes, {info['edge_count']} connections
                    </div>
                </div>
                """

        # Generate JavaScript to update clusters - MOVED OUTSIDE THE LOOP
        update_js = f"""
        function updateClusters() {{
            const clusterData = {dumps(js_clusters_data)};
            Object.keys(clusterData).forEach(key => {{
                clusters[key] = clusterData[key];
            }});

            const clusterList = document.getElementById('clusterList');
            clusterList.innerHTML = `{cluster_html}`;
            updateClusterDisplay();
        }}
        updateClusters();
        """

        # Append this script to the end of the HTML file
        try:
            file_path = path.join(self.web_dir, self.html_filename)
            with open(file_path, 'r', encoding='utf-8') as f:
                html_content = f.read()

            # Replace the updateClusters function
            updated_html = html_content.replace(
                "function updateClusters(clusterData) {\n               // Implementation will be generated by Python\n            }",
                update_js
            )

            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(updated_html)

            logger.info(f"Updated interface with {len(self.clusters)} clusters")
        except Exception as e:
            logger.error(f"Failed to update interface HTML: {e}", exc_info=True)

    def _process_data(self, data_items: Optional[List[Dict[str, Any]]] = None) -> None:
        """
        Verarbeitet Daten aus beiden Quellen (Redis und JSON)

        Args:
           data_items: Liste von Subreddit-Daten, entweder direkt aus Redis oder
                      aufbereitet aus der JSON-Datei
        """
        if data_items is None:
            # Keine Daten angegeben, bestehende Cluster wiederherstellen
            return

        for data in data_items:
            # Extrahiere den Subreddit-Namen
            subreddit_name = data.get("name")
            if not subreddit_name:
                logger.warning(f"Skipping data item without name: {data}")
                continue

            # Bestimme Search Term für dieses Subreddit
            search_term = data.get("search_term", "unknown")

            # Füge es zum entsprechenden Cluster hinzu
            self._add_to_cluster(search_term, data)

            # Legacy-Support: Zum Hauptgraphen hinzufügen
            # Füge Hauptknoten hinzu
            if subreddit_name not in self.added_nodes:
                # Bestimme Knotenfarbe basierend auf NSFW-Status
                color = "#ff0000" if data.get("nsfw", False) else "#00ff00"

                # Berechne Knotengröße basierend auf search_term_count
                weight = min(data.get("search_term_count", 1), self.MAX_WEIGHT)
                size = self.MIN_SIZE + (weight / self.MAX_WEIGHT) * (self.MAX_SIZE - self.MIN_SIZE)

                # Erstelle Tooltip-Text
                title = (f"Related count: {data.get('related_count', 0)}\n"
                         f"Search term count: {data.get('search_term_count', 1)}\n"
                         f"NSFW: {data.get('nsfw', False)}")

                self.net.add_node(
                    subreddit_name,
                    label=subreddit_name,
                    title=title,
                    size=size,
                    color=color
                )
                self.added_nodes.add(subreddit_name)
                logger.info(f"Added node: {subreddit_name}")

            # Verarbeite verwandte Subreddits
            related_subreddits = data.get("related_subreddits", [])
            for related in related_subreddits:
                # Entferne r/ Prefix falls vorhanden
                related_name = related.replace("r/", "") if related.startswith("r/") else related

                # Füge verwandten Knoten hinzu falls noch nicht vorhanden
                if related_name not in self.added_nodes:
                    self.net.add_node(
                        related_name,
                        label=related_name,
                        size=self.MIN_SIZE,
                        color="#ff9999"  # Hellrot für verwandte Knoten
                    )
                    self.added_nodes.add(related_name)

                # Füge Kante hinzu falls noch nicht vorhanden
                edge = tuple(sorted([subreddit_name, related_name]))
                if edge not in self.added_edges:
                    self.net.add_edge(*edge, color="#ffffff")
                    self.added_edges.add(edge)
                    logger.info(f"Added edge: {subreddit_name} -> {related_name}")
            logger.debug(f"Processed data: {data}")
        logger.info(f"_process_data finished processing with {len(data_items) if data_items else 0} items")

    @lru_cache(maxsize=10)
    def load_data_from_json(self, json_path: str) -> Dict[str, Any]:
        """
           Liest eine JSON-Datei und erstellt den Graphen aus der neuen Struktur:
           {
               "root": {
                   "search_term": {
                       "subreddits": [
                           {
                               "name": "subreddit_name",
                               "search_term_count": 1,
                               "related_count": N,
                               "related_subreddits": ["r/sub1", "r/sub2", ...],
                               "nsfw": bool
                           },
                           ...
                       ]
                   },
                   ...
               },
               "metadata": {...}
           }

           Args:
               json_path: Pfad zur JSON-Datei

           Returns:
               Dict: Die geladenen JSON-Daten

           Raises:
               SystemExit: Bei Fehlern beim Laden oder Verarbeiten der Datei
        """
        try:
            with open(json_path, 'r') as f:
                data = json_load(f)

            # Sammle alle Subreddit-Daten
            for search_term, content in data["root"].items():
                for subreddit in content["subreddits"]:
                    # Füge den Search Term zum Subreddit-Datensatz hinzu
                    subreddit["search_term"] = search_term

                    # Füge es zum entsprechenden Cluster hinzu
                    self._add_to_cluster(search_term, subreddit)

            # Update dem Interface-HTML
            self.update_interface_html()

            # Verarbeite für Legacy-Support alle Daten für den Hauptgraphen
            all_subreddits = []
            for search_term, content in data["root"].items():
                for subreddit in content["subreddits"]:
                    subreddit["search_term"] = search_term
                    all_subreddits.append(subreddit)

            self._process_data(all_subreddits)

            # Speichere Zustand für spätere Wiederherstellung
            self.save_state()

            return data
        except FileNotFoundError:
            logger.error(f"JSON file not found: {json_path}")
            self.cleanup()
            raise SystemExit(1)
        except JSONDecodeError:
            logger.error(f"Invalid JSON format in file: {json_path}")
            self.cleanup()
            raise SystemExit(1)
        except Exception as e:
            logger.error(f"Unexpected error while loading JSON: {e}", exc_info=True)
            self.cleanup()
            raise SystemExit(1)

    def merge_selected_clusters(self, cluster_names: List[str]) -> Optional[str]:
        """
        Erstellt eine kombinierte Visualisierung ausgewählter Cluster, indem die
        Originaldaten gefiltert werden.

        Args:
           cluster_names: Liste der sanitierten Namen der auszuwählenden Cluster

        Returns:
           Optional[str]: Relativer Pfad zur HTML-Datei der kombinierten Visualisierung
                         oder None bei Fehler
        """
        if not cluster_names:
            logger.warning("No clusters selected for visualization")
            return None

        # Mapping von sanitierten Namen zu Original-Suchbegriffen
        sanitized_to_original = {
            ''.join(c if c.isalnum() else '_' for c in name): name
            for name in self.clusters.keys()
        }

        # Finde Original-Suchbegriffe für die ausgewählten Cluster
        selected_search_terms = []
        for name in cluster_names:
            if name in self.clusters:
                selected_search_terms.append(name)
            elif name in sanitized_to_original:
                selected_search_terms.append(sanitized_to_original[name])

        if not selected_search_terms:
            logger.error(f"Could not resolve any cluster names from {cluster_names}")
            return None

        logger.info(f"Creating visualization for selected search terms: {selected_search_terms}")

        # Erstelle ein neues Netzwerk für die Visualisierung
        combined_net = self.create_network_with_options()
        combined_nodes = set()
        combined_edges = set()

        # Sammle Subreddit-Daten nur für die ausgewählten Suchbegriffe
        for search_term in selected_search_terms:
            if search_term not in self.clusters:
                continue

            cluster = self.clusters[search_term]

            # Füge alle Knoten und Kanten aus diesem Cluster hinzu
            for node_id in cluster['nodes']:
                combined_nodes.add(node_id)

            for edge in cluster['edges']:
                combined_edges.add(edge)

        # Füge alle gesammelten Knoten zum Netzwerk hinzu
        for node_id in combined_nodes:
            # Suche nach Knotendetails in den ursprünglichen Clustern
            node_details = self._find_node_details(node_id, selected_search_terms)

            if node_details:
                combined_net.add_node(
                    node_id,
                    label=node_details.get('label', node_id),
                    title=node_details.get('title', ''),
                    size=node_details.get('size', self.MIN_SIZE),
                    color=node_details.get('color', '#cccccc')
                )
            else:
                # Fallback für Knoten ohne Details
                combined_net.add_node(
                    node_id,
                    label=node_id,
                    size=self.MIN_SIZE
                )

        # Füge alle gesammelten Kanten zum Netzwerk hinzu
        for source, target in combined_edges:
            if source in combined_nodes and target in combined_nodes:
                combined_net.add_edge(source, target, color="#ffffff")

        # Erstelle einen Dateinamen basierend auf den ausgewählten Suchbegriffen
        filename_parts = [term.replace(' ', '_')[:20] for term in selected_search_terms[:3]]
        filename_base = '_'.join(filename_parts)
        if len(selected_search_terms) > 3:
            filename_base += f"_and_{len(selected_search_terms) - 3}_more"

        # Füge Anzahl der Knoten zum Dateinamen hinzu
        node_count = len(combined_nodes)
        edge_count = len(combined_edges)
        combined_filename = f"combined_{filename_base}_{node_count}nodes_{int(time())}.html"
        file_path = path.join(self.clusters_dir, combined_filename)

        try:
            # Füge Metadaten zum HTML hinzu
            html_content = combined_net.generate_html()
            # Füge den Knotenzähler hinzu
            counter_html = f"""
            <div style="position: fixed; top: 10px; right: 10px; background-color: rgba(0,0,0,0.7); 
                        color: white; padding: 10px; border-radius: 5px; z-index: 1000;">
                Nodes: {node_count} | Edges: {edge_count}
            </div>
            """
            # Füge den Zähler vor dem schließenden </body> Tag ein
            html_content = html_content.replace('</body>', f'{counter_html}</body>')

            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(html_content)

            logger.info(f"Created combined visualization with {node_count} nodes and {edge_count} edges")

            # Gib den relativen Pfad zurück
            return f"clusters/{combined_filename}"
        except Exception as e:
            logger.error(f"Failed to create combined visualization: {e}", exc_info=True)
            return None

    def _find_node_details(self, node_id: str, search_terms: List[str]) -> Optional[Dict[str, Any]]:
        """
        Findet die Details eines Knotens in den angegebenen Clustern.

        Args:
            node_id: ID des zu suchenden Knotens
            search_terms: Liste der Suchbegriffe/Cluster zum Durchsuchen

        Returns:
            Optional[Dict]: Knotendetails oder None, wenn nicht gefunden
        """
        for search_term in search_terms:
            if search_term not in self.clusters:
                continue

            cluster = self.clusters[search_term]
            for node in cluster['network'].nodes:
                if node['id'] == node_id:
                    return node

        return None

    def export_clusters_as_image(
            self,
            cluster_names: List[str],
            resolution: str = "fullhd",
            layout: str = "standard"
    ) -> Optional[str]:
        """
        Exportiert ausgewählte Cluster als hochauflösendes Bild

        Args:
           cluster_names: Liste der zu exportierenden Cluster
           resolution: "fullhd", "4k", "8k" oder "16k"
           layout: "standard" oder "labels"

        Returns:
           Optional[str]: Pfad zur exportierten Datei oder None bei Fehler
        """
        if not cluster_names:
            logger.error("Keine Cluster für Export ausgewählt")
            return None

        # Validierung der Parameter
        valid_resolutions = {"fullhd", "4k", "8k", "16k"}
        valid_layouts = {"standard", "labels"}

        if resolution not in valid_resolutions:
            logger.error(f"Ungültige Auflösung: {resolution}")
            return None

        if layout not in valid_layouts:
            logger.error(f"Ungültiges Layout: {layout}")
            return None

        # Prüfe, ob notwendige Abhängigkeiten vorhanden sind
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from PIL import Image
            from io import BytesIO
        except ImportError as e:
            logger.error(f"Fehlende Abhängigkeit für Export: {e}")
            return None

        # Bildgröße bestimmen
        if resolution == "16k":
            width, height = 15360, 8640  # 16K
        elif resolution == "8k":
            width, height = 7680, 4320  # 8K
        elif resolution == "4k":
            width, height = 3840, 2160  # 4K
        else:  # fullhd
            width, height = 1920, 1080  # Full HD

        # Export-Verzeichnis erstellen
        export_dir = path.join(self.web_dir, "exports")
        makedirs(export_dir, exist_ok=True)

        # Dateipfad generieren
        timestamp = int(time())
        export_filename = f"export_{resolution}_{layout}_{timestamp}.png"
        export_path = path.join(export_dir, export_filename)

        try:
            # Standardansicht oder Labelansicht erstellen
            return self._create_single_image(cluster_names, width, height, layout, export_path)
        except Exception as e:
            logger.error(f"Fehler beim Bildexport: {e}", exc_info=True)
            return None

    def _create_single_image(
            self,
            cluster_names: List[str],
            width: int,
            height: int,
            layout: str,
            export_path: str
    ) -> Optional[str]:
        """
        Erstellt ein einzelnes Bild aus einem oder mehreren kombinierten Clustern

        Args:
        cluster_names: Liste der Cluster-Namen
        width: Bildbreite in Pixeln
        height: Bildhöhe in Pixeln
        layout: "standard" oder "labels"
        export_path: Pfad zum Speichern des Bildes

        Returns:
        Optional[str]: Relativer Pfad zum exportierten Bild oder None bei Fehler
        """
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from PIL import Image
        from io import BytesIO

        # Chrome-Optionen konfigurieren
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-dev-shm-usage")  # Verhindert Speicherprobleme
        chrome_options.add_argument("--no-sandbox")  # Für stabileren Betrieb in Containern
        chrome_options.add_argument(f"--window-size={width},{height}")

        try:
            driver = webdriver.Chrome(options=chrome_options)
        except Exception as e:
            logger.error(f"Failed to initialize Chrome driver: {e}", exc_info=True)
            return None

        try:
            # Erzeugen der Visualisierung durch Zusammenführen ausgewählter Cluster
            combined_view = self.merge_selected_clusters(cluster_names)

            if not combined_view:
                logger.error("Fehler beim Erstellen der kombinierten Ansicht")
                return None

            # Wenn "labels"-Layout gewünscht ist, spezielle Version erstellen
            if layout == "labels":
                labeled_view = self._generate_labeled_html(cluster_names)
                if labeled_view:
                    url = f"http://localhost:{self.port}/{labeled_view}"
                else:
                    # Fallback auf Standard-Ansicht
                    url = f"http://localhost:{self.port}/{combined_view}"
            else:
                url = f"http://localhost:{self.port}/{combined_view}"

            # Seite laden
            driver.get(url)

            # Adaptive Wartezeit basierend auf Auflösung und Layout
            if layout == "labels":
                # Mehr Zeit für Labels-Layout
                wait_time = 60 + (width // 1000)  # Basis 20s + 1s pro 1000px Breite
                wait_time *= len(cluster_names)  # Wait_time x count of clusters
            else:
                wait_time = 60 + (width // 2000)  # Basis 10s + 1s pro 2000px Breite
                wait_time *= len(cluster_names)  # Wait_time x count of clusters

            logger.info(f"Waiting {wait_time}s for rendering to complete...")
            sleep(wait_time)

            # Screenshot erstellen
            png_data = driver.get_screenshot_as_png()

            # Als Bild speichern
            img = Image.open(BytesIO(png_data))
            img.save(export_path)

            logger.info(f"Bild erfolgreich exportiert nach {export_path}")
            return f"exports/{path.basename(export_path)}"
        except Exception as e:
            logger.error(f"Error during image capture: {e}", exc_info=True)
            return None
        finally:
            driver.quit()

    def _generate_labeled_html(self, cluster_names: List[str]) -> Optional[str]:
        """
        Generiert eine spezielle HTML-Ansicht mit großen, gut lesbaren Labels
        für den Bildexport

        Args:
            cluster_names: Liste der Cluster-Namen

        Returns:
            Optional[str]: Relativer Pfad zur generierten HTML-Datei oder None bei Fehler
        """
        # Erst die Originalnamen der Cluster finden
        sanitized_to_original = {
            ''.join(c if c.isalnum() else '_' for c in name): name
            for name in self.clusters.keys()
        }

        selected_search_terms = []
        for name in cluster_names:
            if name in self.clusters:
                selected_search_terms.append(name)
            elif name in sanitized_to_original:
                selected_search_terms.append(sanitized_to_original[name])

        if not selected_search_terms:
            logger.error(f"Could not resolve any cluster names for labeled view: {cluster_names}")
            return None

        # Erstelle ein neues Netzwerk für die zusammengeführten Cluster
        labeled_net = Network(
            height="100vh",
            width="100%",
            bgcolor="#222222",
            font_color="white"
        )

        # Angepasste Optionen für große Labels
        options = {
            "physics": {
                "enabled": True,
                "solver": "forceAtlas2Based",  # Better solver for layout
                "forceAtlas2Based": {
                    "gravitationalConstant": -50,
                    "centralGravity": 0.005,
                    "springLength": 250,
                    "springConstant": 0.05,
                    "damping": 0.4,
                    "avoidOverlap": 1.0
                },
                "stabilization": {
                    "enabled": True,
                    "iterations": 2500,
                    "updateInterval": 25
                },
                "minVelocity": 0.5,
                "maxVelocity": 50
            },
            "nodes": {
                "font": {
                    "size": 24,
                    "face": "Arial",
                    "color": "white",
                    "strokeWidth": 2,
                    "strokeColor": "#222222"
                },
                "scaling": {
                    "min": 30,
                    "max": 80
                },
                "shape": "box",
                "margin": 15,
                "widthConstraint": {
                    "maximum": 150
                },
                "borderWidth": 2
            },
            "edges": {
                "color": {
                    "opacity": 0.7
                },
                "smooth": {
                    "enabled": True,
                    "type": "continuous"
                },
                "width": 1.5
            },
            "layout": {
                "improvedLayout": True  # Disable for better performance
            }
        }

        # Statt set_options zu verwenden, direkt options setzen
        labeled_net.options = options

        # Sammle alle Knoten und Kanten
        combined_nodes = set()
        combined_edges = set()

        for search_term in selected_search_terms:
            if search_term not in self.clusters:
                continue

            cluster = self.clusters[search_term]
            for node_id in cluster['nodes']:
                combined_nodes.add(node_id)

            for edge in cluster['edges']:
                combined_edges.add(edge)

        # Füge alle gesammelten Knoten zum Netzwerk hinzu
        added_nodes = set()
        for node_id in combined_nodes:
            # Suche nach Knotendetails in den ursprünglichen Clustern
            node_details = self._find_node_details(node_id, selected_search_terms)

            if node_id not in added_nodes:
                # Label-Text anpassen (wenn ursprünglich Tooltip-Infos vorhanden sind)
                display_label = node_id
                if node_details and 'title' in node_details:
                    # Tooltip-Infos zum Label hinzufügen
                    info_lines = node_details['title'].split('\n')
                    display_label = f"{node_id}\n{info_lines[0]}"

                # Farbe und Größe bestimmen
                color = node_details['color'] if node_details and 'color' in node_details else '#cccccc'
                size = max(30, node_details['size'] * 1.5) if node_details and 'size' in node_details else 30

                labeled_net.add_node(
                    node_id,
                    label=display_label,
                    title=node_details['title'] if node_details and 'title' in node_details else '',
                    size=size,
                    color=color
                )
                added_nodes.add(node_id)

        # Kanten hinzufügen
        added_edges = set()
        for edge in combined_edges:
            if edge[0] in added_nodes and edge[1] in added_nodes and edge not in added_edges:
                labeled_net.add_edge(edge[0], edge[1], color="#ffffff")
                added_edges.add(edge)

        # HTML speichern
        export_html = f"labeled_export_{int(time())}.html"
        file_path = path.join(self.web_dir, export_html)

        try:
            html_content = labeled_net.generate_html()
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            logger.info(f"Labeled HTML für Export erstellt: {export_html}")
            return export_html
        except Exception as e:
            logger.error(f"Fehler beim Erstellen der Labeled HTML: {e}")
            return None

    def cleanup(self) -> None:
        """
        Räumt Ressourcen auf beim Beenden des Programms.
        Stoppt den HTTP-Server ordnungsgemäß.
        """
        if hasattr(self, 'httpd'):
            self.httpd.shutdown()
            self.httpd.server_close()
            logger.info("HTTP server stopped")
            # Wenn mit Redis verbunden, trenne die Verbindung
            if self.use_server and self.redis_client:
                # Redis leeren, wenn nicht bereits geschehen
                while True:
                    data = self.redis_client.lpop(self.queue_name)
                    if not data:
                        break
                self.redis_client.close()
                logger.info("Redis connection closed")

                # Setze Variablen zurück
                self.added_nodes.clear()
                self.added_edges.clear()

    def run(self) -> None:
        """
        Hauptschleife des Visualisierers.
        Verarbeitet kontinuierlich neue Daten (sofern der Server genutzt wird)
        und aktualisiert den Graphen.
        Kann mit Strg+C beendet werden.
        """
        logger.info("Starting visualization loop...")
        try:
            # Erste Datenverarbeitung
            if self.use_server:
                initial_data_loaded = self.update_graph()
            else:
                initial_data_loaded = True

            # Öffne Browser erst nach dem ersten Update
            if initial_data_loaded:
                webb_open(f'http://localhost:{self.port}/{self.html_filename}')

            # Hauptschleife
            if self.use_server:
                while True:
                    if self.update_graph():
                        continue
                    sleep(2)
            else:
                logger.info("Running in JSON-only mode. Please reload the page manually when needed.")
                while True:
                    sleep(10)
        except KeyboardInterrupt:
            logger.info("Shutting down visualization server...")
            self.cleanup()


def main() -> None:
    """Haupteinstiegspunkt für das Programm"""
    parser = ArgumentParser(description="Reddit Graph Visualizer")
    parser.add_argument("--server", action="store_true",
                        help="Verbindung zu Redis herstellen und Daten abrufen.")
    parser.add_argument("--json", type=str, help="Pfad zu einer JSON-Datei zum einmaligen Laden von Daten.")
    parser.add_argument("--port", type=int, default=8000, help="HTTP-Server-Port (Standard: 8000)")
    args = parser.parse_args()

    if not args.server and not args.json:
        print("Fehler: Du musst entweder --server oder --json <Pfad> angeben.")
        raise SystemExit(1)

    visualizer = RedditGraphVisualizer(use_server=args.server)

    # Set custom port if provided
    if args.port != 8000:
        visualizer.port = args.port

    if args.json:
        visualizer.load_data_from_json(args.json)

    visualizer.run()


if __name__ == "__main__":
    main()