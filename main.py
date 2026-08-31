"""Entry point for the New Relic Anomaly Monitor desktop app."""
from src.gui import App


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
