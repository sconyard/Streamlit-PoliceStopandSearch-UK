# dockerfile using Python image
FROM python:3.9.12-slim

# Create working directory
RUN mkdir /application
WORKDIR "/application"

# upgrade pip
RUN pip install --upgrade pip

# update image
RUN apt-get update \
    && apt-get clean; rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* /usr/share/doc/*

# Copy requirements and script to image working directory
COPY . .

# install requirements
RUN pip install -r /application/requirements.txt

# execute script
ENTRYPOINT ["streamlit", "run"]

CMD ["/application/app.py"]