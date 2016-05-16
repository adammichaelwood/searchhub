#!flask/bin/python
from __future__ import print_function

import json
# Bootstrap the database
from server import app, cmd_args, create_all


# TODO some migration logic

# Bootstrap the Fusion configs (collection, datasources, pipelines, etc)
import json
from os import listdir
from os.path import isfile, join

from server import backend
from server.backends.fusion import new_admin_session

def setup_find_fields(backend, collection_id):
  backend.add_field(collection_id, "publishedOnDate", type="date", required=True)
  backend.add_field(collection_id, "content", type="text_en")
  backend.add_field(collection_id, "project", type="string")
  backend.add_field(collection_id, "body", type="text_en")
  backend.add_field(collection_id, "title", type="text_en")
  backend.add_field(collection_id, "keywords", type="text_en")
  backend.add_field(collection_id, "comments", type="text_en")
  backend.add_field(collection_id, "mimeType", type="string")
  backend.add_field(collection_id, "author_facet", type="string")
  backend.add_field(collection_id, "author", type="text_en", copyDests=["author_facet"])
  backend.add_field(collection_id, "og_description", type="text_en")
  backend.add_field(collection_id, "description", type="text_en")
  backend.add_field(collection_id, "subject", type="text_en")
  backend.add_field(collection_id, "filename_exact", type="string")
  backend.add_field(collection_id, "filename", type="text_en", copyDests=["filename_exact"])
  backend.add_field(collection_id, "length", type="int")
  backend.add_field(collection_id, "isBot", type="boolean")
  backend.add_field(collection_id, "threadId", type="string")
  #backend.add_field(collection_id, "isDocumentation", type="boolean")

# Used for mail threading index
def setup_thread_fields(backend, collection_id):
  backend.add_field(collection_id, "hashId")
  backend.add_field(collection_id, "mailId")
  backend.add_field(collection_id, "parentId")
  backend.add_field(collection_id, "threadId")
  backend.add_field(collection_id, "simpleSubject", type="text_en")
  backend.add_field(collection_id, "depth", type="int")

def setup_pipelines(backend):
  pipe_files = [f for f in listdir("./fusion_config") if isfile(join("./fusion_config", f)) and f.endswith("_pipeline.json")]
  for file in pipe_files: #TODO: what's the python way here?
    print ("Creating Pipeline for %s" % file)
    if file.find("query") != -1:
      backend.create_pipeline(json.load(open(join("./fusion_config", file))), pipe_type="query-pipelines")
    else:
      backend.create_pipeline(json.load(open(join("./fusion_config", file))))


def setup_taxonomy(backend, collection_id):
  status = backend.delete_taxonomy(collection_id)
  taxonomy = json.load(open('fusion_config/taxonomy.json'))
  status = backend.create_taxonomy(collection_id, taxonomy)

def setup_schedules(backend):
  files = [f for f in listdir("./fusion_config") if isfile(join("./fusion_config", f)) and f.endswith("_schedule.json")]
  for file in files:
    print("Creating Schedule for %s" % file)
    backend.create_or_update_schedule(json.load(open(join("./fusion_config", file))))


def setup_projects(backend):
  project_files = [f for f in listdir("./project_config") if isfile(join("./project_config", f)) and f.endswith(".json")]
  if cmd_args.start_datasources:
    print("Each data source created will also be started")
  else:
    print("")
    print("Skipping starting the datasources.  Pass in --start_datasources if you wish to start them when bootstrapping")

  for file in project_files: #TODO: what's the python way here?
    print ("Creating Project for %s" % file)
    project = json.load(open(join("./project_config", file)))
    print("Bootstraping configs for %s..." % project)
    #create the data sources
    datasources = []
    (twitter_config, jira_config, mailbox_configs, wiki_configs, website_configs, github_configs) = backend.create_or_update_datasources(project)
    datasources.append(twitter_config)
    datasources.append(jira_config)
    datasources.extend(mailbox_configs)
    datasources.extend(wiki_configs)
    datasources.extend(website_configs)
    datasources.extend(github_configs)

    for datasource in datasources:
      if datasource:
        # start the data sources
        if cmd_args.start_datasources:
          print ("Stop existing datasource %s if it exists" % datasource["id"])
          backend.stop_datasource(datasource, abort=True)
          print("Starting datasource %s" % datasource["id"])
          #TODO
          backend.start_datasource(datasource["id"])






# TODO bootstrap admin user?
lucidfind_collection_id = app.config.get("FUSION_COLLECTION", "lucidfind")
threads_collection_id = app.config.get("MAIL_THREAD_COLLECTION", "lucidfind_mail_threads")
# Create the "lucidfind" user
if cmd_args.create_collections or create_all:
  session = new_admin_session()
  username = app.config.get("FUSION_APP_USER", "lucidfind")
  status = backend.create_user(username, app.config.get("FUSION_APP_PASSWORD"))
  if status == False:
    exit(1)
  # Create the "lucidfind" collection
  status = backend.create_collection(lucidfind_collection_id, enable_signals=True)
  if status == False:
    exit(1)
  setup_find_fields(backend, lucidfind_collection_id)

  status = backend.create_collection(threads_collection_id)
  if status == False:
    exit(1)
  setup_thread_fields(backend, threads_collection_id)

#create the pipelines
if cmd_args.create_pipelines or create_all:
  setup_pipelines(backend)
  backend.create_query_profile(lucidfind_collection_id, "lucidfind-default", "lucidfind-default")


if cmd_args.create_taxonomy or create_all:
  setup_taxonomy(backend, lucidfind_collection_id)

# Configure each Project

if cmd_args.create_projects or create_all:
  print("Creating Projects")
  setup_projects(backend)

#create the pipelines
if cmd_args.create_schedules or create_all:
  setup_schedules(backend)




