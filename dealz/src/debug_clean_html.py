from bs4 import BeautifulSoup
import sys
import os

def clean_html(input_path: str, output_path: str):
    with open(input_path, 'r', encoding='utf-8') as file:
        soup = BeautifulSoup(file, 'html.parser')

    # Remove script and style tags
    for tag in soup(['script', 'style']):
        tag.decompose()

    # Remove inline styles
    for tag in soup():
        if 'style' in tag.attrs:
            del tag.attrs['style']

    # Save cleaned HTML
    with open(output_path, 'w', encoding='utf-8') as out_file:
        out_file.write(soup.prettify())

    print(f"Cleaned HTML saved to {output_path}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python strip_html.py <input_file.html> <output_file.html>")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2]

    if not os.path.exists(input_file):
        print(f"Error: Input file '{input_file}' not found.")
        sys.exit(1)

    clean_html(input_file, output_file)
