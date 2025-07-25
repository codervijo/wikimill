# Python Playwright in Docker

## 1. Install Python dependencies

Inside your Docker container, run:
```
pip install -r requirements.txt
```

## 2. Install Playwright browsers

After installing the Python package, install the browser binaries:
```
python3 -m playwright install
```

## 3. Run the sample test

You can run the sample test with:
```
pytest test_example.py
```

## 4. How to start Playwright in Docker

- Make sure you are inside the Docker container (e.g., via `docker exec -it <container> bash` or using VSCode Dev Containers).
- Follow steps 1 and 2 above to set up Playwright for Python.
- You can now run any Playwright Python script or tests as shown in step 3.

## Example test

See `test_example.py` for a basic test that launches Chromium, navigates to https://example.com, and checks the page title.

---

**Note:**  
The current Dockerfile is set up for Node.js Playwright. For Python Playwright, you only need Python and the system dependencies (already present). You do NOT need to run `pnpm exec playwright install` for Python projects.
