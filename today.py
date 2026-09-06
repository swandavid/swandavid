import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# ACCESS_TOKEN must be a *classic* personal access token with the `repo` scope.
#
# A fine-grained PAT will not work here. Fine-grained tokens can only reach
# repositories owned by the token's own account, or by an organization that has
# approved them -- they can never reach a private repository on *another user's*
# account where you are only a collaborator. swamsy/nexus-scraper and
# swamsy/nexusodds are exactly that, so a fine-grained token reports 7 repos and
# misses ~660 of my commits. The classic `repo` scope covers collaborator repos.
HEADERS = {'authorization': 'token '+ os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME'] # 'Andrew6rant'
# Optional comma-separated git author emails that are not linked to the GitHub
# account. Commits made with these show up as author.user == null in the API, so
# without listing them here they are silently attributed to nobody.
AUTHOR_EMAILS = [e.strip() for e in os.environ.get('AUTHOR_EMAILS', '').split(',') if e.strip()]
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}
CACHE_COMMENT_SIZE = 7
SKIPPED_COMMITS = 0 # commits GitHub could not produce a diff for

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return 's' if unit != 1 else ''


def post_query(query, variables, retries=4):
    """
    Posts a GraphQL query, retrying with exponential backoff on the transient
    failures GitHub's API is prone to (502/503/504 and secondary rate limits).
    Raises on anything it cannot recover from, so callers never have to guess
    whether a zero means 'no data' or 'the request died'.
    """
    delay = 2
    for attempt in range(retries):
        try:
            response = SESSION.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, timeout=30)
        except requests.exceptions.RequestException as error:
            if attempt == retries - 1:
                raise Exception('GraphQL request failed after retries:', str(error), QUERY_COUNT)
            print(f'Request failed ({error}), retrying in {delay}s... (attempt {attempt + 1}/{retries})')
            time.sleep(delay)
            delay *= 2
            continue

        if response.status_code == 200:
            payload = response.json()
            if payload.get('data') is not None:
                # GitHub returns partial results with an `errors` block for
                # things like 'The additions count for this commit is
                # unavailable' on very large commits. The data is still usable;
                # the affected fields come back null and are read as 0.
                for error in payload.get('errors') or []:
                    print('   partial GraphQL error:', error.get('message'), error.get('path'))
                return payload
            # No data at all -- a real failure, not a partial one. Never let this
            # fall through as a legitimate zero.
            if attempt < retries - 1:
                print(f'Query errored, retrying in {delay}s... (attempt {attempt + 1}/{retries})')
                time.sleep(delay)
                delay *= 2
                continue
            raise Exception('GraphQL query returned errors:', payload.get('errors'), QUERY_COUNT)

        if response.status_code in (403, 429):
            raise Exception('Rate limited by the GitHub API!', response.status_code, response.text, QUERY_COUNT)

        if response.status_code in (502, 503, 504) and attempt < retries - 1:
            print(f'Received {response.status_code}, retrying in {delay}s... (attempt {attempt + 1}/{retries})')
            time.sleep(delay)
            delay *= 2
            continue

        raise Exception('GraphQL query has failed with a', response.status_code, response.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return my total commit count
    """
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date,'end_date': end_date, 'login': USER_NAME}
    payload = post_query(query, variables)
    return int(payload['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def repository_getter(owner_affiliation):
    """
    Uses GitHub's GraphQL v4 API to fetch every repository I own, collaborate on,
    or am an organization member of -- in one paginated pass.

    This used to be four separate queries (repo count, contrib count, star count,
    and the LOC repo list). They all read the same connection, so they are now a
    single walk and the per-affiliation numbers are derived from the result.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    edges, cursor = [], None
    while True:
        payload = post_query(query, {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor})
        repositories = payload['data']['user']['repositories']
        edges += repositories['edges']
        if not repositories['pageInfo']['hasNextPage']:
            return edges
        cursor = repositories['pageInfo']['endCursor']
        query_count('graph_repos_stars')


def stars_counter(edges):
    """
    Count total stars in repositories owned by me
    """
    total_stars = 0
    for edge in edges or []:
        node = (edge or {}).get('node') or {}
        stargazers = node.get('stargazers') or {}
        total_stars += stargazers.get('totalCount', 0)
    return total_stars


def owned_repos(edges):
    """
    Repositories under my own account, i.e. the 'OWNER' affiliation, filtered out
    of the combined repository list rather than re-queried.
    """
    prefix = USER_NAME.lower() + '/'
    return [edge for edge in edges if edge['node']['nameWithOwner'].lower().startswith(prefix)]


def commit_total(node):
    """
    Total commits on a repository's default branch, or None if the repo is empty
    (an empty repo has no default branch at all).
    """
    branch = node.get('defaultBranchRef')
    if not branch:
        return None
    return branch['target']['history']['totalCount']


def author_filters(owner_id):
    """
    Builds the list of GraphQL CommitAuthor filters identifying "me".

    GitHub ANDs the fields of a single CommitAuthor, so an {id, emails} filter
    matches nothing. Each identity therefore needs its own query, and the results
    are de-duplicated by commit oid in case a commit satisfies more than one.
    """
    filters = [{'id': owner_id['id']}]
    if AUTHOR_EMAILS:
        filters.append({'emails': AUTHOR_EMAILS})
    return filters


def repo_loc(owner, repo_name):
    """
    Returns (additions, deletions, my_commits) for one repository.

    The history is filtered author-side by the API instead of pulling every
    commit and matching locally. On a repo like yolov5 that is 19 pages of
    commits down to 1, and it is also the fix for commits authored with an email
    that is not linked to the GitHub account -- those have author.user == null
    and were previously discarded.
    """
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String, $author: CommitAuthor!) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor, author: $author) {
                            edges {
                                node {
                                    ... on Commit {
                                        oid
                                        additions
                                        deletions
                                    }
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    global SKIPPED_COMMITS
    commits = {}
    for author in AUTHOR_FILTERS:
        cursor = None
        while True:
            query_count('recursive_loc')
            variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor, 'author': author}
            branch = post_query(query, variables)['data']['repository']['defaultBranchRef']
            if branch is None: # empty repository, no default branch
                return 0, 0, 0
            history = branch['target']['history']
            for edge in history['edges']:
                node = edge['node']
                if node is None:
                    # GitHub occasionally refuses to compute a diff (usually an
                    # enormous commit) and nulls the whole node. Skip it rather
                    # than dropping the entire repository's counts.
                    SKIPPED_COMMITS += 1
                    continue
                commits[node['oid']] = (node['additions'], node['deletions'])
            if not history['pageInfo']['hasNextPage']:
                break
            cursor = history['pageInfo']['endCursor']

    additions = sum(add for add, _ in commits.values())
    deletions = sum(delete for _, delete in commits.values())
    return additions, deletions, len(commits)


def cache_filename():
    """
    The cache file is named after a hash of the username, so several users can
    share one checkout without clobbering each other.
    """
    return 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'


def read_cache():
    """
    Returns (comment_block, {repo_hash: [total_commits, my_commits, additions, deletions]}).

    Entries are keyed by repo hash rather than by line number. The old positional
    scheme silently skipped a repository whenever its position shifted, leaving
    that repo's counts frozen at whatever they were -- usually zero.
    """
    try:
        with open(cache_filename(), 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        return ['This line is a comment block. Write whatever you want here.\n'] * CACHE_COMMENT_SIZE, {}

    comment = data[:CACHE_COMMENT_SIZE]
    cached = {}
    for line in data[CACHE_COMMENT_SIZE:]:
        fields = line.split()
        if len(fields) == 5:
            cached[fields[0]] = [int(value) for value in fields[1:]]
    return comment, cached


def write_cache(comment, cached, order):
    """
    Writes the cache back out in the order the repositories were returned
    """
    with open(cache_filename(), 'w') as f:
        f.writelines(comment)
        for repo_hash in order:
            f.write(repo_hash + ' ' + ' '.join(str(value) for value in cached[repo_hash]) + '\n')


def loc_query(edges, force_cache=False):
    """
    Returns [additions, deletions, net, all_cached] across every repository.

    A repository is re-counted only when its total commit count has moved since
    the last run, so a normal day costs a handful of queries.
    """
    query_count('loc_query')
    comment, cached = read_cache()
    order, all_cached = [], True

    for edge in edges:
        name = edge['node']['nameWithOwner']
        repo_hash = hashlib.sha256(name.encode('utf-8')).hexdigest()
        order.append(repo_hash)
        total_commits = commit_total(edge['node'])

        if total_commits is None: # empty repository
            cached[repo_hash] = [0, 0, 0, 0]
            continue

        entry = cached.get(repo_hash)
        if entry is not None and entry[0] == total_commits and not force_cache:
            continue

        all_cached = False
        owner, repo_name = name.split('/')
        try:
            additions, deletions, my_commits = repo_loc(owner, repo_name)
        except Exception:
            # Preserve whatever we have rather than zeroing this repo out, then
            # re-raise: a failed request must never be recorded as "0 lines".
            write_cache(comment, cached, [h for h in order if h in cached])
            raise
        cached[repo_hash] = [total_commits, my_commits, additions, deletions]

    write_cache(comment, cached, order)

    loc_add = sum(cached[repo_hash][2] for repo_hash in order)
    loc_del = sum(cached[repo_hash][3] for repo_hash in order)
    return [loc_add, loc_del, loc_add - loc_del, all_cached]


def commit_counter():
    """
    Counts up my total commits, using the cache file written by loc_query.
    """
    _, cached = read_cache()
    return sum(entry[1] for entry in cached.values())


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    Parse SVG files and update elements with my age, commits, stars, repositories, and lines written
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'commit_data', commit_data, 22)
    justify_format(root, 'star_data', star_data, 14)
    justify_format(root, 'repo_data', repo_data, 8)
    justify_format(root, 'age_data', age_data, 49)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 10)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """
    Updates and formats the text of the element, and modifes the amount of dots in the previous element to justify the new text on the svg
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """
    Finds the element in the SVG file and replaces its text with a new value
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def user_getter(username):
    """
    Returns the account ID and creation time of the user
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    payload = post_query(query, {'login': username})
    return {'id': payload['data']['user']['id']}, payload['data']['user']['createdAt']


def follower_getter(username):
    """
    Returns the number of followers of the user
    """
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    payload = post_query(query, {'login': username})
    return int(payload['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    """
    Counts how many times the GitHub GraphQL API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints a formatted time differential
    Returns formatted result if whitespace is specified, otherwise returns raw result
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    """
    David Swan (swandavid)
    """
    print('Calculation times:')
    # define global variable for owner ID and calculate user's creation date
    # e.g {'id': 'MDQ6VXNlcjU3MzMxMTM0'} and 2019-11-03T21:15:07Z for username 'Andrew6rant'
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    AUTHOR_FILTERS = author_filters(OWNER_ID)
    formatter('account data', user_time)

    age_data, age_time = perf_counter(daily_readme, datetime.datetime(2000, 12, 21))
    formatter('age calculation', age_time)

    all_edges, repo_time = perf_counter(repository_getter, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    formatter('repository list', repo_time)
    my_edges = owned_repos(all_edges)
    repo_data = len(my_edges)
    contrib_data = len(all_edges)
    star_data = stars_counter(my_edges)

    total_loc, loc_time = perf_counter(loc_query, all_edges)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)

    commit_data, commit_time = perf_counter(commit_counter)
    formatter('commit counter', commit_time)
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)
    formatter('follower counter', follower_time)

    for index in range(len(total_loc)-1): total_loc[index] = '{:,}'.format(total_loc[index]) # format added, deleted, and total LOC

    svg_overwrite('profile.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])

    print('Total function time:', '{:>11}'.format('%.4f' % (user_time + age_time + repo_time + loc_time + commit_time + follower_time)), ' s')
    if SKIPPED_COMMITS:
        print('Commits skipped (diff unavailable from the API):', SKIPPED_COMMITS)
    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items(): print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))
