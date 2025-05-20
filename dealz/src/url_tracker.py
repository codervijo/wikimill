# url_tracker.py

import os

class URLTracker:
    def __init__(self, file_path="visited_urls.txt"):
        """
        Initialize the URLTracker with an optional file path.

        Args:
            file_path (str): Path to the file for storing visited URLs.
        """
        self._file_path = file_path
        self._visited_urls = set()
        self._load_from_file()

    def _load_from_file(self):
        """
        Load visited URLs from the storage file into the set.
        """
        if os.path.exists(self._file_path):
            with open(self._file_path, "r", encoding="utf-8") as f:
                for line in f:
                    url = line.strip()
                    if url:
                        self._visited_urls.add(url)

    def mark_visited(self, url):
        """
        Mark a URL as visited and append to the backing file.

        Args:
            url (str): The URL to mark as visited.
        """
        if url not in self._visited_urls:
            self._visited_urls.add(url)
            with open(self._file_path, "a", encoding="utf-8") as f:
                f.write(url + "\n")

    def is_url_visited(self, url):
        """
        Check if a URL has been visited.

        Args:
            url (str): The URL to check.

        Returns:
            bool: True if visited, False otherwise.
        """
        return url in self._visited_urls

    def reset(self):
        """
        Clear all visited URLs and remove the backing file.
        """
        self._visited_urls.clear()
        if os.path.exists(self._file_path):
            os.remove(self._file_path)


# ----------------------------
# Example/Test Code (when run directly)
# ----------------------------
if __name__ == "__main__":
    def test_url_tracker():
        print("Testing URLTracker...")

        test_file = "test_urls.txt"
        tracker = URLTracker(test_file)
        tracker.reset()

        assert not tracker.is_url_visited("https://example.com")
        tracker.mark_visited("https://example.com")
        assert tracker.is_url_visited("https://example.com")

        tracker.mark_visited("https://openai.com")
        assert tracker.is_url_visited("https://openai.com")
        assert not tracker.is_url_visited("https://notvisited.com")

        print("Visited URLs so far:")
        for url in ["https://example.com", "https://openai.com", "https://notvisited.com"]:
            print(f"  {url} -> {'YES' if tracker.is_url_visited(url) else 'NO'}")

        # Clean up
        tracker.reset()
        print("All tests passed.\n")

    test_url_tracker()
