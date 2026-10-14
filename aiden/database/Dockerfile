FROM python:3.13-slim
WORKDIR /app
EXPOSE 6004

# Copy database package into /app/database to preserve package structure
COPY ./aiden/database ./database

# Ensure the package marker exists (not checked into source control)
RUN touch database/__init__.py

# Copy shared models
COPY ./shared/backend ./shared/backend

# Install dependencies
RUN pip install --no-cache-dir -r database/requirements.txt

RUN mkdir -p /app/data

# Run the Flask application as a module
CMD ["python", "-m", "database"]
