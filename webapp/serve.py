#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serve.py -- kleiner Entwicklungs-Server fuer die Audio2Midi-WebApp.

Gegenueber 'python -m http.server' zwei wichtige Unterschiede:

  * .js/.mjs werden zuverlaessig mit JavaScript-MIME-Typ ausgeliefert.
    Manche Systeme (z. B. Windows, je nach Registry-Eintrag fuer .js)
    liefern .js sonst als text/plain -- dann lehnt der Browser das Laden
    von ES-Modulen / AudioWorklets ab.
  * Caching ist abgeschaltet (Cache-Control: no-store). So kommen
    Aenderungen beim Neuladen sofort an, ohne haendisches Cache-Leeren.

Start (im Ordner webapp):

    python serve.py            # http://localhost:8000
    python serve.py 8080       # anderer Port
"""

import os
import sys
import functools
import http.server
import socketserver

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

# Immer den Ordner dieser Datei ausliefern -- egal, aus welchem Verzeichnis
# der Server gestartet wird.
WEBROOT = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Im Entwicklungsbetrieb nichts cachen -- immer die frische Datei.
        self.send_header('Cache-Control', 'no-store, max-age=0')
        super().end_headers()


# .js zuverlaessig als JavaScript kennzeichnen (sonst u. U. text/plain).
Handler.extensions_map.update({
    '.js': 'text/javascript',
    '.mjs': 'text/javascript',
})


if __name__ == '__main__':
    socketserver.TCPServer.allow_reuse_address = True
    handler = functools.partial(Handler, directory=WEBROOT)
    with socketserver.TCPServer(('', PORT), handler) as httpd:
        print(f'WebApp laeuft auf http://localhost:{PORT}  (Strg+C zum Beenden)')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nBeendet.')
