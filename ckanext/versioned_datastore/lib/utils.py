import tempfile
from contextlib import contextmanager, closing

import requests
from elasticsearch_dsl import Search, MultiSearch

from ckanext.versioned_datastore.interfaces import IVersionedDatastore
from eevee.config import Config
from eevee.indexing.utils import DOC_TYPE
from eevee.search.search import Searcher

from ckan import plugins, model
from ckan.lib.navl import dictization_functions

DATASTORE_ONLY_RESOURCE = u'_datastore_only_resource'
CSV_FORMATS = [u'csv', u'application/csv']
TSV_FORMATS = [u'tsv']
XLS_FORMATS = [u'xls', u'application/vnd.ms-excel']
XLSX_FORMATS = [u'xlsx', u'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet']
ALL_FORMATS = CSV_FORMATS + TSV_FORMATS + XLS_FORMATS + XLSX_FORMATS

CONFIG = None
SEARCHER = None


def setup_eevee(ckan_config):
    '''
    Given the CKAN config, create the Eevee config object and the eevee Searcher object.

    :param ckan_config: the ckan config
    '''
    global CONFIG
    global SEARCHER

    es_hosts = ckan_config.get(u'ckanext.versioned_datastore.elasticsearch_hosts').split(u',')
    es_port = ckan_config.get(u'ckanext.versioned_datastore.elasticsearch_port')
    prefix = ckan_config.get(u'ckanext.versioned_datastore.elasticsearch_index_prefix')
    CONFIG = Config(
        elasticsearch_hosts=[u'http://{}:{}/'.format(host, es_port) for host in es_hosts],
        elasticsearch_index_prefix=prefix,
        mongo_host=ckan_config.get(u'ckanext.versioned_datastore.mongo_host'),
        mongo_port=int(ckan_config.get(u'ckanext.versioned_datastore.mongo_port')),
        mongo_database=ckan_config.get(u'ckanext.versioned_datastore.mongo_database'),
    )
    SEARCHER = Searcher(CONFIG)


def get_latest_version(resource_id):
    '''
    Retrieves the latest version of the given resource from the status index.

    :param resource_id: the resource's id
    :return: the version or None if the resource isn't indexed
    '''
    index_name = prefix_resource(resource_id)
    return SEARCHER.get_latest_index_versions([index_name]).get(index_name, None)


def validate(context, data_dict, default_schema):
    '''
    Validate the data_dict against a schema. If a schema is not available in the context (under the
    key 'schema') then the default schema is used.

    If the data_dict fails the validation process a ValidationError is raised, otherwise the
    potentially updated data_dict is returned.

    :param context: the ckan context dict
    :param data_dict: the dict to validate
    :param default_schema: the default schema to use if the context doesn't have one
    '''
    schema = context.get(u'schema', default_schema)
    data_dict, errors = dictization_functions.validate(data_dict, schema, context)
    if errors:
        raise plugins.toolkit.ValidationError(errors)
    return data_dict


def prefix_resource(resource_id):
    '''
    Adds the configured prefix to the start of the resource id to get the index name for the
    resource data in elasticsearch.

    :param resource_id: the resource id
    :return: the resource's index name
    '''
    return u'{}{}'.format(CONFIG.elasticsearch_index_prefix, resource_id)


def prefix_field(field):
    '''
    Prefixes a the given field name with "data.". All data from the resource in eevee is stored
    under the data key in the elasticsearch record so to avoid end users needing to know that all
    fields should be referenced by their non-data.-prefixed name until they are internal to the code
    and can be prefixed before being passed on to eevee.

    :param field: the field name
    :return: data.<field>
    '''
    return u'data.{}'.format(field)


def format_facets(aggs):
    '''
    Formats the facet aggregation result into the format we require. Specifically we expand the
    buckets out into a dict that looks like this:

        {
            "facet1": {
                "details": {
                    "sum_other_doc_count": 34,
                    "doc_count_error_upper_bound": 3
                },
                "values": {
                    "value1": 1,
                    "value2": 4,
                    "value3": 1,
                    "value4": 2,
                }
            },
            etc
        }

    etc.

    :param aggs: the aggregation dict returned from eevee/elasticsearch
    :return: the facet information as a dict
    '''
    facets = {}
    for facet, details in aggs.items():
        facets[facet] = {
            u'details': {
                u'sum_other_doc_count': details[u'sum_other_doc_count'],
                u'doc_count_error_upper_bound': details[u'doc_count_error_upper_bound'],
            },
            u'values': {value_details[u'key']: value_details[u'doc_count']
                        for value_details in details[u'buckets']}
        }

    return facets


# this dict stores cached get_field returns. It is only cleared by restarting the server. This is
# safe because the cached data is keyed on the rounded version and is therefore stable as old
# versions of data can't be modified, so the fields will always be valid. If for some reason this
# isn't the case (such as if redactions for specific fields get added later and old versions of
# records are updated) then the server just needs a restart and that's it).
field_cache = {}


def get_fields(resource_id, version=None):
    '''
    Given a resource id, returns the fields that existed at the given version. If the version is
    None then the fields for the latest version are returned.

    The response format is important as it must match the requirements of reclineJS's field
    definitions. See http://okfnlabs.org/recline/docs/models.html#field for more details.

    All fields are returned by default as string types. This is because we have the capability to
    allow searchers to specify whether to treat a field as a string or a number when searching and
    therefore we don't need to try and guess the type and we can leave it to the user to know the
    type which won't cause problems like interpreting a field as a number when it shouldn't be (for
    example a barcode like '013655395'). If we decide that we do want to work out the type we simply
    need to add another step to this function where we count how many records in the version have
    the '.number' subfield - if the number is the same as the normal field count then the field is a
    number type, if not it's a string.

    :param resource_id: the resource's id
    :param version: the version of the data we're querying (default: None, which means latest)
    :return: a list of dicts containing the field data
    '''
    # figure out the index name from the resource id
    index = prefix_resource(resource_id)
    # figure out the rounded version so that we can figure out the fields at the right version
    rounded_version = SEARCHER.get_rounded_versions([index], version)[index]
    # the key for caching should be unique to the resource and the rounded version
    cache_key = (resource_id, rounded_version)

    # if there is a cached version, return it! Woo!
    if cache_key in field_cache:
        return field_cache[cache_key]

    # create a list of field details, starting with the always present _id field
    fields = [{u'id': u'_id', u'type': u'integer'}]
    # lookup the mapping on elasticsearch to get all the field names
    mapping = SEARCHER.elasticsearch.indices.get_mapping(index)[index]
    # if the rounded version response is None that means there are no versions available which
    # shouldn't happen, but in case it does for some reason, just return the fields we have already
    if rounded_version is None:
        return mapping, fields

    # we're only going to return the details of the data fields, collect up these up and sort them
    # TODO: field ordering?
    field_names = sorted(mapping[u'mappings'][DOC_TYPE][u'properties'][u'data'][u'properties'])
    # ignore the _id field, we already know what its deal is
    field_names.remove(u'_id')

    # find out which fields exist in this version and how many values each has
    search = MultiSearch(using=SEARCHER.elasticsearch, index=index)
    for field in field_names:
        # create a search which finds the documents that have a value for the given field at the
        # rounded version. We're only interested in the counts though so set size to 0
        search = search.add(Search().extra(size=0)
                            .filter(u'exists', **{u'field': prefix_field(field)})
                            .filter(u'term', **{u'meta.versions': rounded_version}))

    # run the search and get the response
    responses = search.execute()
    for i, response in enumerate(responses):
        # if the field has documents then it should be included in the fields list
        if response.hits.total > 0:
            fields.append({
                u'id': field_names[i],
                # by default everything is a string
                u'type': u'string',
            })

    # stick the result in the cache for next time
    field_cache[cache_key] = (mapping, fields)

    return mapping, fields


def is_datastore_resource(resource_id):
    '''
    Looks up in elasticsearch whether there is an index for this resource or not and returns the
    boolean result. If there is an index, this is a datastore resource, if not it isn't.

    :param resource_id: the resource id
    :return: True if the resource is a datastore resource, False if not
    '''
    return SEARCHER.elasticsearch.indices.exists(prefix_resource(resource_id))


def is_datastore_only_resource(resource_url):
    '''
    Checks whether the resource url is a datastore only resource url. When uploading data directly
    to the API without using a source file/URL the url of the resource will be set to
    "_datastore_only_resource" to indicate that as such. This function checks to see if the resource
    URL provided is one of these URLs. Note that we check a few different scenarios as CKAN has the
    nasty habit of adding a protocol onto the front of these URLs when saving the resource,
    sometimes.

    :param resource_url: the URL of the resource
    :return: True if the resource is a datastore only resource, False if not
    '''
    return (resource_url == DATASTORE_ONLY_RESOURCE or
            resource_url == u'http://{}'.format(DATASTORE_ONLY_RESOURCE) or
            resource_url == u'https://{}'.format(DATASTORE_ONLY_RESOURCE))


def is_ingestible(resource):
    """
    Returns True if the resource can be ingested into the datastore and False if not. To be
    ingestible the resource must either be a datastore only resource (signified by the url being
    set to _datastore_only_resource) or have a format that we can ingest (the format field on the
    resource is used for this, not the URL).

    :param resource: the resource dict
    :return: True if it is, False if not
    """
    resource_format = resource.get(u'format', None)
    return (is_datastore_only_resource(resource[u'url']) or
            (resource_format is not None and resource_format.lower() in ALL_FORMATS))


@contextmanager
def download_to_temp_file(url, headers=None):
    """
    Streams the data from the given URL and saves it in a temporary file. The (named) temporary file
    is then yielded to the caller for use. Once the context collapses the temporary file is removed.

    :param url: the url to stream the data from
    :param headers: a dict of headers to pass with the request
    """
    headers = headers if headers else {}
    # open up the url for streaming
    with closing(requests.get(url, stream=True, headers=headers)) as r:
        # create a temporary file to store the data in
        with tempfile.NamedTemporaryFile() as temp:
            # iterate over the data from the url stream in chunks
            for chunk in r.iter_content(chunk_size=1024):
                # only write chunks with data in them
                if chunk:
                    # write the chunk to the file
                    temp.write(chunk)
            # the url has been completely downloaded to the temp file, so yield it for use
            temp.seek(0)
            yield temp


def get_public_alias_prefix():
    '''
    Returns the prefix to use for the public aliases.

    :return: the public prefix
    '''
    return u'pub'


def get_public_alias_name(resource_id):
    '''
    Returns the name of the alias which makes gives public access to this resource's datastore data.
    This is just "pub" (retrieved from get_public_alias_prefix above) prepended to the normal
    prefixed index name, for example:

        pubnhm-05ff2255-c38a-40c9-b657-4ccb55ab2feb

    :param resource_id: the resource's id
    :return: the name of the alias
    '''
    return u'{}{}'.format(get_public_alias_prefix(), prefix_resource(resource_id))


def update_resources_privacy(package):
    '''
    Update the privacy of the resources in the datastore associated with the given package. If the
    privacy is already set correctly on each of the resource's indices in Elasticsearch this does
    nothing.

    :param package: the package model object (not the dict!)
    '''
    for resource_group in package.resource_groups_all:
        for resource in resource_group.resources_all:
            update_privacy(resource.id, package.private)


def update_privacy(resource_id, is_private=None):
    '''
    Update the privacy of the given resource id in the datastore. If the privacy is already set
    correctly on the resource's index in Elasticsearch this does nothing.

    :param resource_id: the resource's id
    :param is_private: whether the package the resource is in is private or not. This is an optional
                       parameter, if it is left out we look up the resource's package in the
                       database and find out the private setting that way.
    '''
    if is_private is None:
        resource = model.Resource.get(resource_id)
        is_private = resource.resource_group.package.private
    if is_private:
        make_private(resource_id)
    else:
        make_public(resource_id)


def make_private(resource_id):
    '''
    Makes the given resource private in elasticsearch. This is accomplished by removing the public
    alias for the resource. If the resource's base index doesn't exist at all, or the alias already
    doesn't exist, nothing happens.

    :param resource_id: the resource's id
    '''
    index_name = prefix_resource(resource_id)
    public_index_name = get_public_alias_name(resource_id)
    if SEARCHER.elasticsearch.indices.exists(index_name):
        if SEARCHER.elasticsearch.indices.exists_alias(index_name, public_index_name):
            SEARCHER.elasticsearch.indices.delete_alias(index_name, public_index_name)


def make_public(resource_id):
    '''
    Makes the given resource public in elasticsearch. This is accomplished by adding an alias to the
    resource's index. If the resource's base index doesn't exist at all or the alias already exists,
    nothing happens.

    :param resource_id: the resource's id
    '''
    index_name = prefix_resource(resource_id)
    public_index_name = get_public_alias_name(resource_id)
    if SEARCHER.elasticsearch.indices.exists(index_name):
        if not SEARCHER.elasticsearch.indices.exists_alias(index_name, public_index_name):
            actions = {
                u'actions': [
                    {u'add': {u'index': index_name, u'alias': public_index_name}}
                ]
            }
            SEARCHER.elasticsearch.indices.update_aliases(actions)


def is_resource_read_only(resource_id):
    '''
    Loops through the plugin implementations checking if any of them want the given resource id to
    be read only.

    :return: True if the resource should be treated as read only, False if not
    '''
    implementations = plugins.PluginImplementations(IVersionedDatastore)
    return any(plugin.datastore_is_read_only_resource(resource_id) for plugin in implementations)
