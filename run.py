from app import create_app

app = create_app()

if __name__ == "__main__":
    # debug=False disables the auto-reloader (which restarts on any file change and
    # kills in-flight background job threads) and closes the Werkzeug debugger RCE
    # hole. threaded=True keeps concurrent request handling.
    app.run(host="0.0.0.0", port=5012, debug=False, use_reloader=False, threaded=True)
