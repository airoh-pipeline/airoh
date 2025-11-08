from invoke import task

@task
def make_docs(c, url_logo=None):
    if not url_logo:
        url_logo = "https://avatars.githubusercontent.com/u/215321710?s=400&u=74da437291dece4d451cd5d1003a2aef564721bb&v=4"
        c.run(f"pdoc \
            --output-directory docs \
            --logo \"{url_logo}\" \
            --footer-text 'Copyright airoh contributors 2025'\
            airoh")
