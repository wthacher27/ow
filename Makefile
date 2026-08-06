# ─── Platform detection ───────────────────────────────────────────────────────
ifeq ($(OS),Windows_NT)
    PYTHON  := python
    PIP     := pip
    RM      := del /Q
    MKDIR   := mkdir
    WHICH   := where
    # Docker needs Windows-style absolute path for bind mounts
    DATA_VOL := $(shell cd)\\data
    SEP     := \\
else
    UNAME   := $(shell uname -s)
    PYTHON  := python3
    PIP     := pip3
    RM      := rm -f
    MKDIR   := mkdir -p
    WHICH   := which
    DATA_VOL := $(shell pwd)/data
    SEP     := /
endif

# ─── Variables ────────────────────────────────────────────────────────────────
PLAYER    ?= ""
INTERVAL  ?= 120
MONITOR   ?= 1
LIMIT     ?= 10
MODE      ?= competitive
IMAGE     := ow-tracker

.DEFAULT_GOAL := help

.PHONY: help install install-screen \
        add remove players \
        track watch autotrack \
        read-screen \
        overview heroes ranks ranktrend herotrend \
        log-game map-stats teammate-stats games \
        docker-build docker-run docker-watch docker-autotrack \
        clean

# ─── Help ─────────────────────────────────────────────────────────────────────
help:
	@$(PYTHON) app.py --help

# ─── Setup ────────────────────────────────────────────────────────────────────
install:
	$(PIP) install -r requirements.txt

## Windows gaming PC: also install screen reader deps
install-screen:
	$(PIP) install mss Pillow pytesseract
	@echo ""
	@echo "Tesseract OCR engine also required:"
ifeq ($(OS),Windows_NT)
	@echo "  winget install UB-Mannheim.TesseractOCR"
	@echo "  (or download from https://github.com/UB-Mannheim/tesseract/wiki)"
else ifeq ($(UNAME),Darwin)
	brew install tesseract
else
	sudo apt-get install -y tesseract-ocr
endif

# ─── Player management ────────────────────────────────────────────────────────
add:
	$(PYTHON) app.py add $(PLAYER)

remove:
	$(PYTHON) app.py remove $(PLAYER)

players:
	$(PYTHON) app.py players

search:
	$(PYTHON) app.py search $(PLAYER)

# ─── Tracking ─────────────────────────────────────────────────────────────────
track:
	$(PYTHON) app.py track $(PLAYER)

watch:
	$(PYTHON) app.py watch $(PLAYER) --interval $(INTERVAL)

autotrack:
	$(PYTHON) app.py autotrack --interval $(INTERVAL)

read-screen:
	$(PYTHON) app.py read-screen $(PLAYER) --interval 3 --monitor $(MONITOR)

# ─── Stats display ────────────────────────────────────────────────────────────
overview:
	$(PYTHON) app.py overview $(PLAYER)

heroes:
	$(PYTHON) app.py heroes $(PLAYER) --mode $(MODE) --limit $(LIMIT)

ranks:
	$(PYTHON) app.py ranks $(PLAYER)

map-stats:
	$(PYTHON) app.py map-stats $(PLAYER)

teammate-stats:
	$(PYTHON) app.py teammate-stats $(PLAYER)

games:
	$(PYTHON) app.py games $(PLAYER) --limit $(LIMIT)

# ─── Docker ───────────────────────────────────────────────────────────────────
docker-build:
	docker build -t $(IMAGE) .

docker-run:
	docker run --rm -v "$(DATA_VOL):/app/data" $(IMAGE) $(CMD)

docker-watch:
	docker run -v "$(DATA_VOL):/app/data" $(IMAGE) watch $(PLAYER) --interval $(INTERVAL)

docker-autotrack:
	docker run -v "$(DATA_VOL):/app/data" $(IMAGE) autotrack --interval $(INTERVAL)

# ─── Cleanup ──────────────────────────────────────────────────────────────────
clean:
ifeq ($(OS),Windows_NT)
	if exist __pycache__ rmdir /S /Q __pycache__
	if exist *.pyc $(RM) *.pyc
else
	$(RM) -r __pycache__ *.pyc
endif
