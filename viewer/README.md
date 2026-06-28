# LedgerViewer

A standalone, **read-only** macOS app (Apple Silicon / arm64) that visualizes the
VRM pipeline's derivation **forest** — every generated VRM and how it descends
from its parents — as an interactive tree.

It is the graphical counterpart to `ledger tree` (the Rust CLI's ASCII forest):
same data, drawn as a pannable/zoomable tree with thumbnails and a detail panel.

## Requirements

- Apple Silicon Mac (arm64). The project builds **arm64-only**.
- Xcode 16+ (Swift 5/6). Verified with Xcode 16.1 / Swift 6.0.2.

## Build & run

From this directory:

```sh
cd LedgerViewer
xcodebuild -project LedgerViewer.xcodeproj -scheme LedgerViewer \
  -configuration Release -arch arm64 -sdk macosx \
  -derivedDataPath ./DerivedData build
open ./DerivedData/Build/Products/Release/LedgerViewer.app
```

Or just open `LedgerViewer/LedgerViewer.xcodeproj` in Xcode and press Run.

> The Xcode **scheme is autocreated** by `xcodebuild` (no scheme is committed),
> and the app is **ad-hoc signed** (`CODE_SIGN_IDENTITY = "-"`) so it builds
> headlessly without a developer team. `DerivedData/` and `build/` are gitignored.

## Data source

The app reads the ledger SQLite database at:

```
~/.vrm-pipeline/ledger.db
```

(the same default the Rust pipeline writes — `ledger`'s `default_db_path`). It is
opened with `SQLITE_OPEN_READONLY` and parsed in `LedgerStore.swift`. If the DB
does not exist yet, the app shows guidance rather than failing — it populates as
you generate VRMs.

A schema-decoupled alternative contract also exists: `ledger export --json`
emits the whole forest as JSON (records with `parent_id`).

## Features

- **Tree** — tidy (Reingold–Tilford) layout, forest roots side by side, elbow
  edges, pinch-zoom (0.2–3×) and drag-pan.
- **Thumbnails** — each node shows a render from the record's `r0_ref` dir
  (the pipeline's WebP face views: `face_front`, `body_front`, …), or a
  placeholder when absent.
- **Inspector** — select a node for prompt, outcome metrics (adopted,
  edit distances), tags, file paths (with Reveal in Finder), and generation params.
- **Search / filter** — by prompt, id prefix, or tag, plus an "Adopted only"
  toggle; matches highlight while their ancestors stay for context.
- **Live refresh** — the view reloads as the pipeline writes new records
  (directory FS events + a SQLite header change-counter poll).

## Read-only guarantee

The viewer **never writes to the ledger**. The database is opened read-only, and
the only OS interaction that touches the filesystem is "Reveal in Finder" on an
asset/render path. There are no insert/update/delete code paths.

## Verification

`LedgerStore` and `TreeLayout` are pure Foundation/CoreGraphics (no SwiftUI), so
they are checked headlessly without launching the GUI:

```sh
cd LedgerViewer
./Tests/run.sh [path-to-ledger.db]   # defaults to ~/.vrm-pipeline/ledger.db
```

It prints record/root counts, confirms the forest partition, and asserts layout
invariants (no node overlap, children below parents, parents centered).

## Source layout

```
LedgerViewer/LedgerViewer/
  LedgerViewerApp.swift   app entry
  ContentView.swift       load states + tree/inspector host
  LedgerStore.swift       read-only SQLite reader + forest builder (no SwiftUI)
  TreeLayout.swift        tidy tree layout (no SwiftUI)
  TreeView.swift          Canvas edges + node cards, pan/zoom, search styling
  Thumbnail.swift         r0_ref render thumbnail loader (WebP via NSImage)
  InspectorView.swift     read-only detail panel
  LedgerWatcher.swift     live-refresh file watcher
LedgerViewer/Tests/
  main.swift, run.sh      headless read-layer + layout verification
```
