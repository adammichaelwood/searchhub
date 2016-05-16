import json
import requests
from collections import OrderedDict
from dictdiffer import diff
from server.backends.github_helper import create_github_datasource_configs
from server.backends.jira_helper import create_jira_datasource_config
from server.backends.mailbox_helper import create_mailinglist_datasource_configs
from server.backends.twitter_helper import create_twitter_datasource_configs
from server.backends.website_helper import create_website_datasource_configs
from server.backends.wiki_helper import create_wiki_datasource_configs
from urlparse import urljoin

from server import app
from server.backends import Backend, Document


class FusionSession(requests.Session):
  """
  Wrapper around requests.Session that manages a cookie-based session
  """

  def __init__(self, proxy_url, username, password, lazy=False):
    super(FusionSession, self).__init__()
    self.__base_url = proxy_url
    self.proxy_url = proxy_url
    self.username = username
    self.password = password
    if not lazy:
      self._authenticate()

  def _authenticate(self):
    headers = {"Content-type": "application/json"}
    data = {'username': self.username, 'password': self.password}
    resp = self.post("session", data=json.dumps(data), headers=headers)
    if resp.status_code == 201:
      pass
    else:
      raise Exception("failed to authenticate, check credentials")

  def request(self, method, url, **kwargs):
    full_url = urljoin(self.__base_url, url)
    resp = super(FusionSession, self).request(method, full_url, **kwargs)
    if resp.status_code == 401:
      if url == "session":
        return resp
      else:
        print("session expired, re-authenticating")
        self._authenticate()
        return super(FusionSession, self).request(method, full_url, **kwargs)
    else:
      return resp


class FusionBackend(Backend):
  def __init__(self):
    # TODO: this should come from the configs.
    self.admin_session = FusionSession(
      app.config.get("FUSION_URL", "http://localhost:8764/api/"),
      app.config.get("FUSION_ADMIN_USERNAME"),
      app.config.get("FUSION_ADMIN_PASSWORD")
    )
    self.app_session = FusionSession(
      app.config.get("FUSION_URL", "http://localhost:8764/api/"),
      app.config.get("FUSION_APP_USERNAME"),  # TODO change to another user
      app.config.get("FUSION_APP_PASSWORD"),
      lazy=True
    )

  def add_field(self, collection_name, name, type="string", required=False, multivalued=False, indexed=True,
                stored=True, defaultVal=None, copyDests=None):
    data = {
      "name": name,
      "type": type,
      "required": required,
      "multiValued": multivalued,
      "indexed": indexed,
      "stored": stored,
      "default": defaultVal
    }
    if copyDests:
      data["copyDests"] = copyDests
    resp = self.admin_session.post("apollo/collections/{0}/schema/fields".format(collection_name),
                                   data=json.dumps(data))

  def send_signal(self, collection_id, payload):
    """
    Send a signal
    """
    resp = self.admin_session.get("apollo/signals/{0}/i".format(collection_id),
                                  # tack on the i so that we invoke the snowplow endpoint
                                  params=payload)
    if resp.status_code != 200:
      print "Unable to send signal: {0}".format(resp.text)
      return False
    return True

  def create_user(self, username, password):
    resp = self.admin_session.get("users")
    exists = False
    for user in resp.json():
      if user['username'] == username:
        exists = True
        break
    if not exists:
      # Create User
      print("Creating %s user... " % username)
      resp = self.admin_session.post("users",
                                     data=json.dumps({
                                       "username": username,
                                       "password": password,
                                       "passwordConfirm": password,  # TODO: don't hardcode this
                                       "realmName": "native",
                                       "roleNames": ["collection-admin"]  # TODO figure out correct permissions
                                     }),
                                     headers={'Content-Type': "application/json"})
      if resp.status_code == 201:
        print("ok")
      else:
        print("failed")
        print(resp.text)
        return False
    else:
      # User exists
      print("User %s exists, doing nothing" % username)
    return True

  def create_collection(self, collection_id, enable_signals=False, enable_search_logs=True, enable_dynamic_schema=True):
    resp = self.admin_session.get("apollo/collections/{0}".format(collection_id))
    if resp.status_code == 404:
      # Create
      print("Creating Collection {0}... ".format(collection_id))
      resp = self.admin_session.post("apollo/collections", data=json.dumps({'id': collection_id}),
                                     headers={'Content-Type': "application/json"})
      if resp.status_code == 200:
        print("ok")
        if enable_signals:
          print "Enabling Signals"
          sig_resp = self.admin_session.put("apollo/collections/{0}/features/signals".format(collection_id),
                                            data='{"enabled":true}',
                                            headers={'Content-Type': "application/json"})
          print sig_resp.status_code
        if enable_search_logs:
          self.admin_session.put("apollo/collections/{0}/features/searchLogs".format(collection_id),
                                 data='{"enabled":true}',
                                 headers={'Content-Type': "application/json"})
        if enable_dynamic_schema:
          self.admin_session.put("apollo/collections/{0}/features/dynamicSchema".format(collection_id),
                                 data='{"enabled":true}',
                                 headers={'Content-Type': "application/json"})
      else:
        print("failed")
        print(resp.text)
        return False
    elif resp.status_code == 200:
      print("Collection {0} exists, doing nothing".format(collection_id))
    else:
      print("Collection API error, aborting")
      print(resp.text)
      return False
    return True

  def create_query_profile(self, collection_id, name, pipeline_name):

    resp = self.admin_session.put("apollo/collections/{0}/query-profiles/{1}".format(collection_id, name),
                                  data='"' + pipeline_name + '"',
                                  headers={"Content-type": "application/json"})

    if resp.status_code != 204:
      print resp.status_code, resp.json()
    return resp

  def create_pipeline(self, pipeline_config, pipe_type="index-pipelines"):
    id = pipeline_config["id"]
    print "create pipeline: " + id
    resp = self.admin_session.put("apollo/{0}/{1}".format(pipe_type, id), data=json.dumps(pipeline_config),
                                  headers={"Content-type": "application/json"})

    if resp.status_code != 200:
      print resp.status_code, resp, json.dumps(pipeline_config)
    resp = self.admin_session.put("apollo/{0}/{1}/refresh".format(pipe_type, id),
                                  headers={"Content-type": "application/json"})
    if resp.status_code != 204:
      print resp.status_code, resp.json()
    return resp

  def create_or_update_datasources(self, project):
    twitter_config = None
    jira_config = None
    mailbox_configs = []
    wiki_configs = []
    website_configs = []
    github_configs = []
    # Generate twitter datasources
    if "twitter" in project and app.config.get('TWITTER_CONSUMER_KEY'):
      twitter_config = create_twitter_datasource_configs(project)
      # print twitter_config['id']
      self.update_datasource(**twitter_config)
    # JIRA
    if "jira" in project:
      jira_config, sched = create_jira_datasource_config(project)
      self.update_datasource(**jira_config)
      self.create_or_update_schedule(sched)
    # Generate Mailboxes
    if "mailing_lists" in project:
      mailbox_configs, schedules = create_mailinglist_datasource_configs(project)
      for config in mailbox_configs:
        self.update_datasource(**config)
      for schedule in schedules:
        self.create_or_update_schedule(schedule)
    # Generate Wikis
    if "wikis" in project:
      wiki_configs, schedules = create_wiki_datasource_configs(project)
      for config in wiki_configs:
        self.update_datasource(**config)
      for schedule in schedules:
        self.create_or_update_schedule(schedule)
    # Generate Githubs
    if "githubs" in project:
      github_configs, schedules = create_github_datasource_configs(project)
      for config in github_configs:
        self.update_datasource(**config)
      for schedule in schedules:
        self.create_or_update_schedule(schedule)
    # Generate Websites
    if "websites" in project:
      website_configs, schedules = create_website_datasource_configs(project)
      for config in website_configs:
        self.update_datasource(**config)
      for schedule in schedules:
        self.create_or_update_schedule(schedule)
    #TODO: should we return schedules?
    # TODO: flatten this out
    # Add in the PUTS
    return (twitter_config, jira_config, mailbox_configs, wiki_configs, website_configs, github_configs)





  def _from_solr_doc(self, solr_doc):
    """
    Convert a document coming back from Solr to
    """
    return Document(id=solr_doc.get("id"), author=solr_doc.get("author"), source=solr_doc.get("source"),
                    project=solr_doc.get("project"), content=solr_doc.get("content"),
                    created_at=solr_doc.get("created_at"), link=solr_doc.get("link"))

  def get_document(self, doc_id):
    path = "apollo/query-pipelines/{1}/collections/{2}/select".format("default", "lucidfind")
    params = {
      "q": "*:*",
      "fq": "id:{0}".format(doc_id),
      "wt": "json"
    }
    resp = self.app_session.get(path, params=params, headers={"Content-type": "application/json"})
    return self._from_solr_doc(resp.json()['response']['docs'][0])

  def find_documents(self, query="*", source=None, author=None, project=None, limit=10, offset=0):
    path = "apollo/query-pipelines/{0}/collections/{1}/select".format("default", "lucidfind")
    # TODO move this to a QP config?
    params = {
      "q": query,
      "defType": "edismax",
      "qf": ["author_t^10", "person_t^8", "content_t^6", "source_s^4", "project^4"],
      "fl": ["id:id", "author:author_s", "source:source_s", "project:project", "content:content_t",
             "created_at:created_at_dt", "link:url"],
      "fq": [],
      "rows": limit,
      "start": offset,
      "facet": True,
      "facet.mincount": 1,
      "facet.limit": 20,
      "facet.order": "count",
      "facet.field": ["source_s", "person_ss", "project"],
      "wt": "json",
      "json.nl": "arrarr"
    }

    if source is not None:
      params['fq'].append("source_s:{0}".format(source))
    if author is not None:
      params['fq'].append("author_s:{0}".format(author))
    if project is not None:
      params['fq'].append("project:{0}".format(project))
    params['fq'].append("content_t:*")  # TODO is this a bug in the field mapper "set" op?

    resp = self.app_session.get(path, params=params, headers={"Content-type": "application/json"})

    decoded = resp.json()
    docs = [self._from_solr_doc(doc) for doc in decoded['response']['docs']]

    facets = decoded['facet_counts']['facet_fields']
    ordered_facets = OrderedDict()
    for field, field_facets in facets.items():
      # TODO rename facet fields?
      ordered_facets[field] = OrderedDict()
      for value, count in field_facets:
        ordered_facets[field][value] = count

    found = decoded['response']['numFound']
    return docs, ordered_facets, found

  def delete_taxonomy(self, collection_id, category=None):
    if category:
      resp = self.admin_session.delete("apollo/collections/{0}/taxonomy/{1}".format(collection_id, category))
      print resp.status_code
    else:
      # get all the categories at the top and delete them
      resp = self.admin_session.get("apollo/collections/{0}/taxonomy".format(collection_id))
      if resp.status_code == 200:
        tax = resp.json()
        for category in tax:
          print "Deleting: {0}".format(category["id"])
          resp = self.admin_session.delete("apollo/collections/{0}/taxonomy/{1}".format(collection_id, category["id"]))
      elif resp.status_code == 404:
        pass  # do nothing, as there is no taxonomy
      else:
        raise Exception("Couldn't get {1} taxonomy for {0}".format(collection_id, resp.status_code))

  def create_taxonomy(self, collection_id, taxonomy):
    print "Creating taxonomy for {0}".format(collection_id)

    resp = self.admin_session.post("apollo/collections/{0}/taxonomy".format(collection_id), data=json.dumps(taxonomy),
                                   headers={"Content-type": "application/json"})
    if resp.status_code == 404:
      return None
    elif resp.status_code == 200:
      return resp.json()
    else:
      print resp.status_code
      print resp.text
      raise Exception("Couldn't create taxonomy for {0}.  Tax: {1}".format(collection_id, taxonomy))

  def create_or_update_schedule(self, schedule):
    # check to see if it exists already
    resp = self.admin_session.get("apollo/scheduler/schedules/{0}".format(schedule["id"]))
    if resp.status_code == 200:
      print "Updating schedule for {0}".format(schedule["id"])
      resp = self.admin_session.put("apollo/scheduler/schedules/{0}".format(schedule["id"]), data=json.dumps(schedule),
                                   headers={"Content-type": "application/json"})
      if resp.status_code == 204:
        return None  #TODO: better code here?
      else:
        print resp.status_code
        print resp.text
        raise Exception("Couldn't update schedule for {0}.  Schedule: {1}".format(schedule["id"], schedule))
    elif resp.status_code == 404:
      print "Creating schedule for {0}".format(schedule["id"])
      resp = self.admin_session.post("apollo/scheduler/schedules", data=json.dumps(schedule),
                                   headers={"Content-type": "application/json"})
      if resp.status_code == 200:
        return resp.json()
      else:
        print resp.status_code
        print resp.text
        raise Exception("Couldn't create schedule for {0}.  Schedule: {1}".format(schedule["id"], schedule))
    return None

  def get_datasource(self, id):
    resp = self.admin_session.get("apollo/connectors/datasources/{0}".format(id))
    if resp.status_code == 404:
      return None
    elif resp.status_code == 200:
      return resp.json()

  def update_datasource(self, id, **config):
    """
    Update a datasource if it has changed
    """
    datasource = self.get_datasource(id)
    config['id'] = id

    if datasource is None:
      # Create it
      resp = self.admin_session.post("apollo/connectors/datasources",
                                     data=json.dumps(config),
                                     headers={"Content-type": "application/json"})
      if resp.status_code != 200:
        raise Exception("Could not create Datasource %s: %s \n%s" % (id, resp.text, json.dumps(config)))
    else:
      # Update it (maybe)
      if compare_datasources(config, datasource) == False:
        print("Detected an update in config, updating Fusion")
        resp = self.admin_session.put("apollo/connectors/datasources/{0}".format(id),
                                      data=json.dumps(config),
                                      headers={"Content-type": "application/json"})
        # TODO check response
      else:
        print "No change in datasource, doing nothing"

  def start_datasource(self, id):
    datasource = self.get_datasource(id)
    if datasource is not None:
      resp = self.admin_session.post("apollo/connectors/jobs/{0}".format(id))
      return resp.json()
    else:
      raise Exception("Could not start Datasource %s" % (id))

  def stop_datasource(self, id, abort=False):
    datasource = self.get_datasource(id)
    if datasource is not None:
      resp = self.admin_session.delete("apollo/connectors/jobs/{0}?abort={1}".format(id, str(abort).lower()))
      return resp.json()


def _new_session(proxy_url, username, password):
  "Establishes a cookie-based session with the Fusion proxy node"
  session = FusionSession(proxy_url, username, password)
  return session


def new_admin_session():
  return _new_session(
    app.config.get("FUSION_URL", "http://localhost:8764/api/"),
    app.config.get("FUSION_ADMIN_USERNAME"),
    app.config.get("FUSION_ADMIN_PASSWORD")
  )


def new_user_session():
  return _new_session(
    app.config.get("FUSION_URL", "http://localhost:8764/api/"),
    app.config.get("FUSION_APP_USERNAME"),
    app.config.get("FUSION_APP_PASSWORD")
  )


def compare_datasources(test_datasource, target_datasource):
  """
  Test if test_datasource is a subset of target_datasource

  :param test_datasource: the datasource to test
  :param target_datasource: the target datasource for comparison with
  :returns: True if test_datasource is a subset of target_datasource, False otherwise
  """

  is_subset = True
  for change in diff(test_datasource, target_datasource):
    if change[0] != "add":
      is_subset = False
      break
  return is_subset