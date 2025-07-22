from flask import Flask, request, render_template, abort
import shortuuid
import os
from pygments import highlight
from pygments.lexers import get_lexer_by_name, get_all_lexers
from pygments.formatters import HtmlFormatter
 
app = Flask(__name__)
 
# Directory to store paste files
PASTE_DIR = 'pastes'
if not os.path.exists(PASTE_DIR):
    os.makedirs(PASTE_DIR)
 
# Function to get available programming languages for syntax highlighting
def get_language_options():
    return sorted([(lexer[1][0], lexer[0]) for lexer in get_all_lexers() if lexer[1]])
 
# Route for the main page
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        # Get content and language from the form
        content = request.form['content']
        language = request.form['language']
        # Generate a unique ID for the paste
        paste_id = shortuuid.uuid()
        # Create the file path for the paste
        file_path = os.path.join(PASTE_DIR, paste_id)
 
        # Save the paste content to a file
        with open(file_path, 'w') as f:
            f.write(f"{language}\n{content}")
 
        # Generate the URL for the new paste
        paste_url = request.url_root + paste_id
        return render_template('index.html', paste_url=paste_url, languages=get_language_options())
 
    # Render the form with available languages
    return render_template('index.html', languages=get_language_options())
 
# Route to view a specific paste by its ID
@app.route('/<paste_id>')
def view_paste(paste_id):
    # Create the file path for the paste
    file_path = os.path.join(PASTE_DIR, paste_id)
    if not os.path.exists(file_path):
        abort(404)  # Return a 404 error if the paste does not exist
 
    # Read the paste file
    with open(file_path, 'r') as f:
        language = f.readline().strip()  # First line is the language
        content = f.read()  # Remaining content is the paste
 
    # Get the appropriate lexer for syntax highlighting
    lexer = get_lexer_by_name(language, stripall=True)
    # Create a formatter for HTML output
    formatter = HtmlFormatter(linenos=True, cssclass="source")
    # Highlight the content
    highlighted_content = highlight(content, lexer, formatter)
    # Get the CSS for the highlighted content
    highlight_css = formatter.get_style_defs('.source')
 
    # Render the paste with syntax highlighting
    return render_template('index.html', paste_content=highlighted_content, highlight_css=highlight_css)
 
if __name__ == '__main__':
    app.run(debug=True)