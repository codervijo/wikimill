#!/bin/bash

# Script to run Playwright's code generation tool

# Default values
URL="https://playwright.dev"
OUTPUT_FILE="generated.py"
LANGUAGE="python"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  key="$1"
  case $key in
    -u|--url)
      URL="$2"
      shift
      shift
      ;;
    -o|--output)
      OUTPUT_FILE="$2"
      shift
      shift
      ;;
    -l|--language)
      LANGUAGE="$2"
      shift
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [options]"
      echo "Options:"
      echo "  -u, --url URL         Website URL to visit (default: https://playwright.dev)"
      echo "  -o, --output FILE     Output file for generated code"
      echo "  -l, --language LANG   Language for code generation (javascript, python, java, csharp)"
      echo "  -h, --help            Show this help message"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# Validate language
if [[ ! "$LANGUAGE" =~ ^(javascript|python|java|csharp)$ ]]; then
  echo "Error: Invalid language. Choose from: javascript, python, java, csharp"
  exit 1
fi

# Ensure URL has protocol
if [[ ! "$URL" =~ ^https?:// ]]; then
  echo "URL does not include protocol (http:// or https://). Adding https:// prefix..."
  URL="https://$URL"
fi

# Build the codegen command with additional options for better compatibility
CODEGEN_CMD="npx playwright codegen --target $LANGUAGE $URL --timeout 60000"

# Add output file if specified
if [[ -n "$OUTPUT_FILE" ]]; then
  CODEGEN_CMD="$CODEGEN_CMD --output $OUTPUT_FILE"
fi

# Add browser option for better compatibility
CODEGEN_CMD="$CODEGEN_CMD --browser chromium"

# Run the command
echo "Starting Playwright code generation..."
echo "URL: $URL"
echo "Language: $LANGUAGE"
if [[ -n "$OUTPUT_FILE" ]]; then
  echo "Output file: $OUTPUT_FILE"
fi
echo ""
echo "Running: $CODEGEN_CMD"
echo ""
echo "Playwright will open a browser window. Interact with the website to generate code."
echo "Close the browser when finished to complete code generation."
echo ""

# Execute the command
$CODEGEN_CMD
