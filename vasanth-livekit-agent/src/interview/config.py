"""Configuration for interview-plan retrieval."""

SUPPORTED_LANGUAGES = ("html", "java", "javascript", "python", "react")
DEFAULT_DOMAINS = ["react", "javascript"]

DEFAULT_STARTER_CODE = {
    "html": (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "  <head>\n"
        '    <meta charset="UTF-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        "    <title>Frontend coding question</title>\n"
        "  </head>\n"
        "  <body>\n"
        '    <main id="app">\n'
        "      <!-- Build your interface here. -->\n"
        "    </main>\n"
        "    <script>\n"
        "      // Add your JavaScript here.\n"
        "    </script>\n"
        "  </body>\n"
        "</html>\n"
    ),
    "javascript": "// Write your solution here.\n",
    "react": (
        'import React from "react";\n\n'
        "export default function App() {\n"
        "  return (\n"
        "    <main>\n"
        "      {/* Implement your solution here. */}\n"
        "    </main>\n"
        "  );\n"
        "}\n"
    ),
}

COUNTS = {
    "0-3": {"verbal": 6, "coding": 2, "machine": 3, "system-design": 1},
    "4-8": {"verbal": 5, "coding": 2, "machine": 2, "system-design": 1},
}

DIFFICULTIES = {
    "0-3": ["easy", "medium"],
    "4-8": ["medium", "hard"],
}

BUCKET_TYPES = {
    "verbal": ["verbal", "mcq"],
    "coding": ["coding", "code-output"],
    "machine": ["machine-coding"],
    "system-design": ["verbal"],
}

BUCKET_ORDER = ["verbal", "coding", "machine", "system-design"]
MACHINE_CODING_SOURCE_CONTEXT = "mock-interview"
