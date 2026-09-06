import datetime
from dateutil import relativedelta
import requests
import os
import time
import hashlib
import base64
import fnmatch
import render

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
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'loc_query': 0, 'commit_files': 0, 'profile_stats': 0}
CACHE_COMMENT_SIZE = 7
EXCLUDE_PATHS = [] # populated from cache/exclude_paths.txt at startup

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


def load_excludes():
    """
    Globs of generated / vendored / bulk-data paths that should not count as
    authored code. Kept in cache/exclude_paths.txt so the list can be tuned
    without touching this file.
    """
    patterns = []
    try:
        with open('cache/exclude_paths.txt', 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    patterns.append(line.lower())
    except FileNotFoundError:
        pass
    return patterns


def is_excluded(path):
    """
    True if a file path matches any exclude glob
    """
    path = path.lower()
    return any(fnmatch.fnmatch(path, pattern) for pattern in EXCLUDE_PATHS)


def commit_shas(owner, repo_name, stop_at=None):
    """
    Returns (shas, complete) -- my non-merge commit shas on the default branch,
    newest first.

    Merge commits are dropped because GitHub reports a merge's `additions` as the
    whole combined diff, which re-counts every line of the branch being merged.
    That alone roughly triples the total.

    If `stop_at` is given, the walk stops as soon as that sha is seen and
    `complete` comes back False, meaning the caller holds a valid running total
    to add to. If the walk finishes without seeing it (a rebase, a force push, or
    a first run) `complete` is True and the caller must recount from scratch.
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
                                        parents { totalCount }
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
    seen, shas = set(), []
    for author in AUTHOR_FILTERS:
        cursor = None
        while True:
            query_count('recursive_loc')
            variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor, 'author': author}
            branch = post_query(query, variables)['data']['repository']['defaultBranchRef']
            if branch is None: # empty repository, no default branch
                return [], True
            history = branch['target']['history']
            for edge in history['edges']:
                node = edge['node']
                if node is None:
                    continue
                if node['oid'] == stop_at:
                    return shas, False
                if node['parents']['totalCount'] > 1: # merge commit
                    continue
                if node['oid'] not in seen:
                    seen.add(node['oid'])
                    shas.append(node['oid'])
            if not history['pageInfo']['hasNextPage']:
                break
            cursor = history['pageInfo']['endCursor']
    return shas, True


def commit_loc(owner, repo_name, sha):
    """
    Returns (additions, deletions) for one commit, counting only files that
    survive the exclude list.

    The file list is only available from the REST API -- GraphQL exposes a
    commit's totals but not its per-file breakdown -- so this costs one request
    per commit. Results are cached per repository, and only commits newer than
    the last cached one are ever fetched.
    """
    query_count('commit_files')
    url = f'https://api.github.com/repos/{owner}/{repo_name}/commits/{sha}'
    delay = 2
    for attempt in range(4):
        response = SESSION.get(url, timeout=30)
        if response.status_code == 200:
            break
        if response.status_code in (502, 503, 504) and attempt < 3:
            time.sleep(delay)
            delay *= 2
            continue
        if response.status_code == 422: # commit too large for the API to diff
            print('   commit diff unavailable:', owner + '/' + repo_name, sha[:8])
            return 0, 0
        raise Exception('commit fetch failed', response.status_code, url, response.text[:200])
    else:
        raise Exception('commit fetch failed after retries', url)

    additions = deletions = 0
    for changed in response.json().get('files') or []:
        if is_excluded(changed['filename']):
            continue
        additions += changed.get('additions', 0)
        deletions += changed.get('deletions', 0)
    return additions, deletions


def repo_loc(owner, repo_name, cached_entry=None):
    """
    Returns (my_commits, additions, deletions, newest_sha) for one repository,
    resuming from the cached entry when the history still lines up.
    """
    stop_at = cached_entry[4] if cached_entry and len(cached_entry) > 4 and cached_entry[4] != '-' else None
    shas, complete = commit_shas(owner, repo_name, stop_at)

    if complete or cached_entry is None:
        commits = additions = deletions = 0 # full recount
    else:
        _, commits, additions, deletions, _ = cached_entry

    for sha in shas:
        add, delete = commit_loc(owner, repo_name, sha)
        additions += add
        deletions += delete
    commits += len(shas)

    newest = shas[0] if shas else (stop_at or '-')
    return commits, additions, deletions, newest


def cache_filename():
    """
    The cache file is named after a hash of the username, so several users can
    share one checkout without clobbering each other.
    """
    return 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'


def read_cache():
    """
    Returns (comment_block, {repo_hash: [total_commits, my_commits, additions, deletions, newest_sha]}).

    Entries are keyed by repo hash rather than by line number. The old positional
    scheme silently skipped a repository whenever its position shifted, leaving
    that repo's counts frozen at whatever they were -- usually zero. Rows written
    by an older version have five fields instead of six and are treated as having
    no resume point, so they get recounted once.
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
        if len(fields) == 6:
            cached[fields[0]] = [int(v) for v in fields[1:5]] + [fields[5]]
    return comment, cached


def write_cache(comment, cached, order):
    """
    Writes the cache back out in the order the repositories were returned
    """
    with open(cache_filename(), 'w') as f:
        f.writelines(comment)
        for repo_hash in order:
            f.write(repo_hash + ' ' + ' '.join(str(v) for v in cached[repo_hash]) + '\n')


def loc_query(edges, force_cache=False):
    """
    Returns [additions, deletions, net, all_cached] across every repository.

    A repository is only revisited when its total commit count has moved since
    the last run, and even then only its new commits are fetched.
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
            cached[repo_hash] = [0, 0, 0, 0, '-']
            continue

        entry = cached.get(repo_hash)
        if entry is not None and entry[0] == total_commits and not force_cache:
            continue

        all_cached = False
        owner, repo_name = name.split('/')
        try:
            commits, additions, deletions, newest = repo_loc(owner, repo_name, None if force_cache else entry)
        except Exception:
            # Preserve whatever we have rather than zeroing this repo out, then
            # re-raise: a failed request must never be recorded as "0 lines".
            write_cache(comment, cached, [h for h in order if h in cached])
            raise
        cached[repo_hash] = [total_commits, commits, additions, deletions, newest]

    write_cache(comment, cached, order)

    loc_add = sum(cached[h][2] for h in order)
    loc_del = sum(cached[h][3] for h in order)
    return [loc_add, loc_del, loc_add - loc_del, all_cached]


def commit_counter():
    """
    Counts up my total commits, using the cache file written by loc_query.
    """
    _, cached = read_cache()
    return sum(entry[1] for entry in cached.values())


def profile_stats(login):
    """
    Language breakdown and the last year of contribution activity, in one query.

    Languages are weighted by bytes, the same measure GitHub's own repo bars use,
    and carry GitHub's official brand colour for each language.
    """
    query_count('profile_stats')
    query = '''
    query($login: String!) {
        user(login: $login) {
            contributionsCollection {
                contributionCalendar {
                    totalContributions
                    weeks {
                        contributionDays { contributionCount date }
                    }
                }
            }
            repositories(first: 100, ownerAffiliations: [OWNER, COLLABORATOR]) {
                edges {
                    node {
                        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
                            edges { size node { name color } }
                        }
                    }
                }
            }
        }
    }'''
    user = post_query(query, {'login': login})['data']['user']

    sizes, colors = {}, {}
    for edge in user['repositories']['edges']:
        for lang in edge['node']['languages']['edges']:
            name = lang['node']['name']
            sizes[name] = sizes.get(name, 0) + lang['size']
            colors[name] = lang['node']['color'] or '#8b949e'
    total = sum(sizes.values()) or 1
    languages = [
        {'name': name, 'pct': 100.0 * size / total, 'color': colors[name]}
        for name, size in sorted(sizes.items(), key=lambda item: -item[1])
    ]

    calendar = user['contributionsCollection']['contributionCalendar']
    weeks = [sum(day['contributionCount'] for day in week['contributionDays'])
             for week in calendar['weeks']]
    return {
        'languages': languages,
        'weeks': weeks,
        'contributions': calendar['totalContributions'],
    }


def avatar_data_uri(login):
    """
    Fetches the account's avatar and returns it as a data: URI.

    An SVG rendered through GitHub's image proxy cannot reference anything
    external, so the portrait has to be embedded. Reading it from the account
    means it tracks whatever avatar is set, with no file to keep in sync.
    """
    try:
        response = SESSION.get(f'https://github.com/{login}.png?size=400', timeout=30)
        response.raise_for_status()
        return 'data:image/png;base64,' + base64.b64encode(response.content).decode('ascii')
    except requests.exceptions.RequestException as error:
        print('   avatar unavailable, falling back to initials:', error)
        return None


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
    EXCLUDE_PATHS = load_excludes()

    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    AUTHOR_FILTERS = author_filters(OWNER_ID)
    formatter('account data', user_time)

    age_data, age_time = perf_counter(daily_readme, datetime.datetime(2000, 12, 21))
    formatter('age calculation', age_time)

    all_edges, repo_time = perf_counter(repository_getter, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    formatter('repository list', repo_time)
    my_edges = owned_repos(all_edges)

    total_loc, loc_time = perf_counter(loc_query, all_edges)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)

    commit_data, commit_time = perf_counter(commit_counter)
    formatter('commit counter', commit_time)
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)
    formatter('follower counter', follower_time)
    stats, stats_time = perf_counter(profile_stats, USER_NAME)
    formatter('languages/activity', stats_time)
    avatar, avatar_time = perf_counter(avatar_data_uri, USER_NAME)
    formatter('avatar', avatar_time)

    render.write('profile.svg', {
        'name': 'David Swan',
        'login': USER_NAME,
        'role': 'AI Research Engineer',
        'company': 'Lockheed Martin Space',
        'avatar': avatar,
        'info': [
            ('uptime', age_data.replace(' years,', 'y').replace(' months,', 'm').replace(' days', 'd').replace(' year,', 'y').replace(' month,', 'm').replace(' day', 'd')),
            ('focus', 'Robotics, Webscraping'),
            ('stack', 'Python, C++, PyTorch'),
            ('editor', 'VSCode, Nvim'),
            ('speaks', 'English, Spanish'),
            ('email', 'david.soccer.swan@gmail.com'),
            ('linkedin', 'd-swan'),
            ('discord', 'swanadavid'),
        ],
        'repos': len(my_edges),
        'contrib': len(all_edges),
        'commits': commit_data,
        'followers': follower_data,
        'stars': stars_counter(my_edges),
        'loc_add': total_loc[0],
        'loc_del': total_loc[1],
        'loc_net': total_loc[2],
        'languages': stats['languages'],
        'weeks': stats['weeks'],
        'contributions': stats['contributions'],
    })

    print('Total function time:', '{:>11}'.format('%.4f' % (user_time + age_time + repo_time + loc_time + commit_time + follower_time + stats_time + avatar_time)), ' s')
    print('Total GitHub API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items(): print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))
