# Reddit Graph Crawler and Visualizer

## Overview

This project consists of a Reddit crawler and visualizer that collects subreddit relationships and visualizes them as interactive network graphs. The tool helps analyze connections between subreddits by crawling Reddit data and creating dynamic network visualizations.

## Features

- **Systematic Reddit Crawling**: Searches Reddit for subreddits matching generated search terms
- **Relationship Analysis**: Identifies connections between subreddits by analyzing descriptions
- **Real-time Visualization**: Creates interactive network graphs showing subreddit relationships
- **Clustering**: Groups related subreddits into clusters by search term
- **High-Resolution Export**: Exports visualizations in multiple resolutions (up to 16K)
- **Data Persistence**: Saves crawled data in JSON format for later analysis
- **Optional Redis Integration**: Supports real-time updates via Redis

## Components

### 1. Main Crawler (`main.py`)
The crawler component searches Reddit for subreddits and analyzes their relationships.

### 2. Visualization Tool (`visualization.py`)
The visualization component creates interactive network graphs from the collected data.

### 3. Simple Redis Manager (`popOS_redis_manager.sh`)
A utility script to manage the Redis server for data communication between components.

## Data Collection

The crawler collects the following data:
- **Subreddit names**: Names of subreddits matching search terms
- **Related subreddits**: Mentions of other subreddits in descriptions
- **NSFW status**: Whether subreddits are marked as NSFW
- **Search term associations**: Which search terms led to discovering each subreddit

All data is publicly available information from Reddit. No private user data is collected.

## Data Visualization

The visualization component represents:
- **Nodes**: Subreddits as network nodes
- **Edges**: Relationships between subreddits
- **Node size**: Relevance to search terms
- **Node color**: NSFW status (red for NSFW, green for SFW)

The tool provides:
- **Cluster views**: Groups subreddits by search terms
- **Combined views**: Merges multiple clusters
- **Interactive exploration**: Pan, zoom, and select nodes
- **High-resolution exports**: Generate images for publication

## Setup and Usage

### Prerequisites
- Python 3.10+
- Redis server (optional, for real-time updates)
- Reddit API credentials

### Installation

1. Clone the repository:
```bash
git clone https://github.com/yourusername/reddit-graph-visualizer.git
cd reddit-graph-visualizer
```
2. Install dependencies:
```bash
pip install -r requirements.txt
```
Create a .env file with your Reddit API credentials:
```bash
REDDIT_CLIENT_ID=your_client_id
REDDIT_CLIENT_SECRET=your_client_secret
REDDIT_USER_AGENT=your_user_agent_name
SAVE_DATA_PATH=reddit_crawl_data.json
SEARCH_TERM_LENGTH=1
SEARCH_LIMIT=10
```
### Running the Crawler
```bash
python3 main.py
```
Add the '--server' flag if you want to use Redis for real-time visualization.

### Running the Visualization Tool
```bash
python visualization.py [--server] [--json path/to/data.json]
```

### Managing Redis (if using server mode)
```bash
./popOS_redis_manager.sh start
./popOS_redis_manager.sh status
./popOS_redis_manager.sh stop
```

## License
This project is licensed under the GNU General Public License v3.0 (GPL-3.0)

This means that:
- You can use, modfy, and distribute the code freely
- If you distribute modified versions, you must:
  - Make your source code available
  - License your code under GPL-3.0
  - Document your changes

## Technical Details
### Crawler Architecture
- Multi-threaded search for maximum efficiency
- Queue-based processing of related subreddits
- Thread-safe data structures with proper locking
- TkInter GUI for monitoring and control

### Visualization Architecture
* Force-directed graph layout
* Customizable physics settings
* Cluster-based organization
* Browser-based interface
* High-resolution export capability
* Adaptive rendering for different data sizes

### System Compatibility
This software has only been tested on Ubuntu Linux. It may work on other Linux distributions or operating systems, but compatibility is not guaranteed. This project is developed with help of A.I.


### Contributing
Contributions are welcome! Please feel free to submit a Pull Request.

### Disclaimer
This tool is intended for educational and research purposes only. Please respect Reddit's terms of service and API usage guidelines.

