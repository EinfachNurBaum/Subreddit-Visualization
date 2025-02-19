from re import finditer
import praw
from threading import Lock, Thread
from queue import Queue, Empty
from time import strftime
from collections import defaultdict
from itertools import product
from string import ascii_lowercase
from json import dump, load
from logging import getLogger, basicConfig as lbasicConfig, INFO as lINFO
import tkinter as tk
from dotenv import load_dotenv
from os import getenv
import atexit
from redis import Redis, ConnectionError, RedisError
from pickle import dumps as pdumps

# Logging Konfiguration
lbasicConfig(
    level=lINFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='reddit_crawler.log'
)
logger = getLogger(__name__)


def generate_search_terms(max_length=3):
    """
    Generiert Suchbegriffe systematisch.
    """
    for length in range(1, max_length + 1):
        for combo in product(ascii_lowercase, repeat=length):
            yield ''.join(combo)


class CrawlerGUI:
    """GUI für Crawler-Steuerung und Status-Anzeige"""

    def __init__(self, crawler):
        self.crawler = crawler
        self.root = tk.Tk()
        self.root.configure(bg='black')
        self.root.title("Reddit Crawler")

        self.root.minsize(400, 300)

        self.root.rowconfigure(2, weight=1)
        self.root.columnconfigure(0, weight=1)

        self.status_label = tk.Label(
            self.root,
            text="Crawler läuft...",
            fg="white",
            bg="black"
        )
        self.status_label.grid(row=0, pady=10, sticky="ew")

        self.stop_button = tk.Button(
            self.root,
            text="Stop Crawler",
            command=self.stop_crawler,
            bg="red",
            fg="white"
        )
        self.stop_button.grid(row=1, pady=10)

        self.status_lines = []
        self.max_lines = 5
        self.thread_status = tk.Text(
            self.root,
            width=50,
            bg="black",
            fg="green",
            wrap=tk.WORD
        )
        self.thread_status.grid(row=2, padx=10, pady=10, sticky="nsew")
        self.thread_status.config(state='disabled')

        # Bind das Resize-Event
        self.root.bind('<Configure>', self.on_resize)

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.update_status()

    def on_resize(self, event):
        """Passt die Anzahl der sichtbaren Zeilen basierend auf der Fenstergröße an"""
        if event.widget == self.root:
            # Berechne verfügbare Höhe für Text Widget
            available_height = event.height - self.status_label.winfo_height() - self.stop_button.winfo_height() - 40

            # Berechne Anzahl der möglichen Zeilen (1 Zeile ≈ 20 Pixel Höhe)
            line_height = 20
            self.max_lines = max(1, available_height // line_height)

            # Update Text Widget
            self.update_text_content()

    def update_text_content(self):
        """Aktualisiert den Textinhalt basierend auf der aktuellen max_lines"""
        self.thread_status.config(state='normal')
        self.thread_status.delete(1.0, tk.END)

        # Zeige nur so viele Zeilen an, wie in das Fenster passen
        for line in self.status_lines[:self.max_lines]:
            self.thread_status.insert(tk.END, line + '\n')

        self.thread_status.config(state='disabled')

    def add_status_line(self, new_line):
        """Fügt eine neue Statuszeile hinzu"""
        self.status_lines.insert(0, new_line)
        self.update_text_content()

    def update_status(self):
        if hasattr(self.crawler, 'current_status'):
            current_status = self.crawler.current_status
            if not self.status_lines or current_status != self.status_lines[0]:
                self.add_status_line(current_status)
        self.root.after(1000, self.update_status)

    def stop_crawler(self):
        self.crawler.running = False
        self.status_label.config(text="Stoppe Crawler...")
        self.add_status_line("Crawler wird gestoppt...")
        self.crawler.cleanup()
        self.add_status_line("-----------------")
        self.status_label.config(text="Crawler gestoppt")

    def on_closing(self):
        self.stop_crawler()
        self.root.destroy()


class SaveManager(Thread):
    def __init__(self, save_path):
        super().__init__()
        self.save_path = save_path
        self.save_queue = Queue()
        self.running = True
        self.daemon = True
        self.lock = Lock()
        self.backup_counter = 0
        self.BACKUP_FREQUENCY = 10  # Backup alle 10 Speichervorgänge

    def run(self):
        while self.running:
            try:
                data_item = self.save_queue.get(timeout=1)
                if data_item is None:
                    break

                try:
                    with open(self.save_path, "r") as f:
                        current_data = load(f)
                except FileNotFoundError:
                    current_data = {"root": {}, "metadata": {}}

                if data_item.get("type") == "update":
                    self._update_subreddit_data(current_data, data_item["data"])
                else:
                    self._add_new_subreddit(
                        current_data,
                        data_item["search_term"],
                        data_item["subreddit_data"]
                    )

                current_data["metadata"]["last_update"] = strftime("%Y-%m-%d %H:%M:%S")

                with open(self.save_path, "w") as f:
                    dump(current_data, f, indent=2)

                # Backup nur periodisch erstellen
                self.backup_counter += 1
                if self.backup_counter >= self.BACKUP_FREQUENCY:
                    with open(f"{self.save_path}.backup", "w") as f:
                        dump(current_data, f, indent=2)
                    self.backup_counter = 0

            except Empty:
                continue
            except Exception as e:
                logger.error(f"Fehler beim Speichern: {e}")

    def _update_subreddit_data(self, current_data, update_data):
        """Aktualisiert Daten eines existierenden Subreddits"""
        for search_term in current_data["root"]:
            for subreddit in current_data["root"][search_term]["subreddits"]:
                if subreddit["name"] == update_data["name"]:
                    # Behalte NSFW-Status bei Update bei
                    nsfw_status = subreddit.get("nsfw", False)
                    subreddit.update({
                        "related_subreddits": update_data["related_subreddits"],
                        "related_count": update_data["related_count"],
                        "nsfw": nsfw_status  # Behalte NSFW-Status
                    })
                    return

    def _add_new_subreddit(self, current_data, search_term, subreddit_data):
        """Fügt einen neuen Subreddit hinzu"""
        if search_term not in current_data["root"]:
            current_data["root"][search_term] = {"subreddits": []}

        if "nsfw" in subreddit_data:
            subreddit_data["nsfw"] = True

        current_data["root"][search_term]["subreddits"].append(subreddit_data)

    def stop(self):
        """Stoppt den SaveManager sauber"""
        self.running = False
        self.save_queue.put(None)  # Sende Shutdown Signal


class RedditCrawler:
    def __init__(self, search_terms=None, use_server=False):
        # Lade Umgebungsvariablen
        load_dotenv()

        self.SAVE_PATH = getenv('SAVE_DATA_PATH', 'reddit_crawl_data.json')

        # Initialisiere PRAW
        self.reddit = praw.Reddit(
            client_id=getenv('REDDIT_CLIENT_ID'),
            client_secret=getenv('REDDIT_CLIENT_SECRET'),
            user_agent=getenv('REDDIT_USER_AGENT', 'MyRedditCrawler v1.0')
        )

        self.current_status = "Initialisierung..."
        if search_terms is None:
            max_length = int(getenv('SEARCH_TERM_LENGTH', "1"))
            search_terms = list(generate_search_terms(max_length=max_length))
        else:
            search_terms = list(search_terms)

        if not search_terms:
            raise ValueError("Keine Suchbegriffe vorhanden")

        # Direkte Aufteilung ohne separate Längen-Variable
        middle = len(search_terms) // 2
        self.first_half = search_terms[:middle]
        self.second_half = search_terms[middle:]

        self.subreddit_queue = Queue()
        self.results = defaultdict(int)
        self.connections = defaultdict(lambda: defaultdict(int))
        self.processed_subreddits = set()
        self.lock = Lock()
        self.running = True

        self.last_processed = {
            'thread1': None,
            'thread2': None,
            'related_thread': None
        }

        # Initialize Redis connection
        self.use_server = use_server
        if use_server:
            self.redis_client = None
            self.setup_redis_connection()
            self.graph_queue = "reddit_graph_queue"

            # Clear any existing data in the queue
            self.redis_client.delete(self.graph_queue)

        # Initialisiere SaveManager
        self.save_manager = SaveManager(self.SAVE_PATH)

        atexit.register(self.cleanup)
        logger.info(f"Reddit Crawler initialisiert mit {middle} Suchbegriffen")

    def setup_redis_connection(self):
        """Establishes connection to Redis with error handling"""
        try:
            self.redis_client = Redis(
                host='localhost',
                port=6379,
                db=0,
                socket_connect_timeout=5
            )
            # Test the connection
            self.redis_client.ping()
            logger.info("Successfully connected to Redis")
        except ConnectionError as e:
            logger.error(f"Could not connect to Redis: {e}")
            logger.error("Please ensure Redis server is running:")
            logger.error("  - On Ubuntu/Debian: sudo service redis-server start")
            logger.error("  - On Windows WSL: sudo service redis-server start")
            raise SystemExit(1)

    def search_subreddits(self, search_term, thread_name):
        """Sucht Subreddits mit PRAW"""
        self.current_status = f"🔍 Suche nach: {search_term} in {thread_name}"
        logger.info(f"Starte Suche für: {search_term}")

        try:
            # Suche nach Subreddits mit PRAW
            subreddits = self.reddit.subreddits.search(search_term, limit=int(getenv('SEARCH_LIMIT', 10)))

            for subreddit in subreddits:
                if not self.running:
                    break

                with self.lock:
                    if subreddit.display_name not in self.processed_subreddits:
                        # Bereite Daten für die Speicherung vor
                        subreddit_data = {
                            "name": subreddit.display_name,
                            "search_term_count": 1,
                            "related_count": 0,
                            "related_subreddits": [],
                            "nsfw": subreddit.over18
                        }

                        # Sende an Save-Queue
                        self.save_manager.save_queue.put({
                            "type": "new",  # Kennzeichnung für neuen Subreddit
                            "search_term": search_term,
                            "subreddit_data": subreddit_data
                        })

                        # Füge zur Verarbeitung hinzu
                        self.subreddit_queue.put(subreddit.display_name)
                        self.processed_subreddits.add(subreddit.display_name)

                        if self.use_server and self.redis_client:
                            try:
                                graph_data = {
                                    "node": subreddit.display_name,
                                    "weight": 1,
                                    "nsfw": subreddit.over18,
                                    "search_term": search_term
                                }
                                self.redis_client.rpush(
                                    self.graph_queue,
                                    pdumps(graph_data)
                                )
                            except RedisError as e:
                                logger.error(f"Redis error during search: {e}")

            with self.lock:
                self.last_processed[thread_name] = search_term

            logger.info(f"Suche für {search_term} abgeschlossen")

        except Exception as e:
            logger.error(f"Fehler bei der Suche nach {search_term}: {e}")
            self.last_processed[thread_name] = f"ERROR: {search_term}"

    def process_related_subreddits(self):
        # Regex Pattern für r/subreddit Erwähnungen
        subreddit_pattern = r'(?:^|\s)(?:/?r/|/r/)([a-zA-Z0-9_]+)'

        while self.running:
            try:
                subreddit_name = self.subreddit_queue.get(timeout=10)
                self.current_status = f"🔗 Verarbeite verwandte Subreddits für: {subreddit_name}"

                try:
                    subreddit = self.reddit.subreddit(subreddit_name)
                    if not subreddit.subreddit_type == 'public':
                        continue
                    description = subreddit.description

                    # Finde alle Subreddit Erwähnungen mit Regex
                    related_subs = set(
                        f"r/{match.group(1)}"
                        for match in finditer(subreddit_pattern, description)
                    )

                    # Filtere den aktuellen Subreddit aus den Related heraus
                    related_subs.discard(f"r/{subreddit_name}")

                    if related_subs:
                        # Bereite Update-Daten vor
                        update_data = {
                            "name": subreddit_name,
                            "related_subreddits": list(related_subs),
                            "related_count": len(related_subs),
                            "nsfw": subreddit.over18  # NSFW-Status hinzufügen
                        }

                        # Sende Update an SaveManager
                        self.save_manager.save_queue.put({
                            "type": "update",
                            "data": update_data
                        })
                        # Optional Redis Update
                        if self.use_server and self.redis_client:
                            try:
                                graph_data = {
                                    "node": subreddit_name,
                                    "connections": list(related_subs),
                                    "nsfw": subreddit.over18
                                }
                                self.redis_client.rpush(
                                    self.graph_queue,
                                    pdumps(graph_data)
                                )
                            except RedisError as e:
                                logger.error(f"Redis error in related processing: {e}")

                    for related_name in related_subs:
                        if not self.running:
                            break

                        with self.lock:
                            self.connections[subreddit_name][related_name] += 1
                            self.connections[related_name][subreddit_name] += 1
                            self.results[related_name] += 1

                except Exception as e:
                    logger.warning(f"Fehler beim finden von Related Subreddits bei {subreddit_name}: {e}")

            except Empty:
                logger.info("Queue ist leer. Beende Related Subreddits Verarbeitung.")
                break

    def cleanup(self):
        """Cleanup-Methode"""
        logger.info("Führe Cleanup durch...")
        # Stoppe SaveManager
        self.save_manager.stop()
        self.save_manager.join()

        if self.use_server and self.redis_client:
            try:
                # Lösche die Queue
                self.redis_client.delete(self.graph_queue)
                # Schließe Redis Verbindung
                self.redis_client.close()
                logger.info("Redis Verbindung geschlossen")
            except RedisError as e:
                logger.error(f"Redis error during cleanup: {e}")
            except Exception as e:
                logger.error(f"An error occurred during cleanup: {e}")

        # Leere alle Datenstrukturen
        self.results.clear()
        self.connections.clear()
        self.processed_subreddits.clear()

        # Leere die Queue
        while not self.subreddit_queue.empty():
            try:
                self.subreddit_queue.get_nowait()
            except Empty:
                break

        # Setze alle relevanten Variablen zurück
        self.last_processed = {
            'thread1': None,
            'thread2': None,
            'related_thread': None
        }

        self.running = False
        self.current_status = "Beendet"
        logger.info("Cleanup abgeschlossen")

    def search_worker(self, search_terms, thread_name):
        """Worker-Thread für die Suche"""
        for term in search_terms:
            if not self.running:
                break
            self.search_subreddits(term, thread_name)

    def start_crawling(self):
        """Startet den Crawling-Prozess"""
        logger.info("Starte Crawling-Prozess...")

        gui = CrawlerGUI(self)

        self.save_manager.start()

        search_thread1 = Thread(
            target=self.search_worker,
            args=(self.first_half, "thread1")
        )
        search_thread2 = Thread(
            target=self.search_worker,
            args=(self.second_half, "thread2")
        )
        related_thread = Thread(
            target=self.process_related_subreddits
        )

        logger.info("Threads werden gestartet")
        search_thread1.start()
        search_thread2.start()
        related_thread.start()

        gui.root.mainloop()

        search_thread1.join()
        search_thread2.join()
        related_thread.join()

        self.cleanup()
        logger.info("Crawling-Prozess abgeschlossen")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', action='store_true', help='Enable Redis server features')
    args = parser.parse_args()

    crawler = RedditCrawler(use_server=args.server)
    crawler.start_crawling()


if __name__ == "__main__":
    main()