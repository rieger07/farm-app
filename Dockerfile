# Use a slim version of Python to keep the image size small
FROM python:3.11-alpine

# Set the working directory inside the container
WORKDIR /app

# Since we only use streamlit, we can install it directly
RUN pip3 install streamlit streamlit-vis-timeline setuptools

# Copy your local code into the container
COPY . .

# Expose the default Streamlit port
EXPOSE 8501

# Configure healthcheck to ensure the container is running correctly
HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health

# Command to run the app
ENTRYPOINT ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
