import urllib.request


def main() -> None:
    urllib.request.urlopen("http://localhost:8000/health", timeout=2).read()


if __name__ == "__main__":
    main()
