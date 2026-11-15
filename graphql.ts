import type { IncomingMessage, ServerResponse } from 'node:http';
import { readJson, sendJson } from './http.ts';
import { Store, HttpError, type Repository, type RecordData } from './store.ts';

type Selection = { name: string; alias: string; args: RecordData; fields: Selection[]; directives: { name: string; args: RecordData }[]; fragment?: string; on?: string };
type Operation = { kind: string; name?: string; defaults: RecordData; fields: Selection[] };
type Document = { operations: Operation[]; fragments: Map<string, { on: string; fields: Selection[] }> };
class Parser {
  tokens: string[]; at = 0;
  constructor(source: string) {
    this.tokens = [];
    const re = /\s+|#[^\r\n]*|,|\.\.\.|"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|[_A-Za-z][_0-9A-Za-z]*|[!$():=@\[\]{}|&]/gy;
    let pos = 0;
    while (pos < source.length) {
      re.lastIndex = pos; const m = re.exec(source);
      if (!m) throw new Error(`Unexpected character at ${pos}`);
      if (!/^(?:\s|#|,)/.test(m[0])) this.tokens.push(m[0]);
      pos = re.lastIndex;
    }
    if (this.tokens.length > 100000) throw new Error('Query too large');
  }
  peek(): string { return this.tokens[this.at] ?? ''; }
  take(expected?: string): string {
    const token = this.tokens[this.at++];
    if (!token || expected && token !== expected) throw new Error(`Expected ${expected ?? 'token'}, found ${token ?? 'end of query'}`);
    return token;
  }
  value(depth = 0): any {
    if (depth > 64) throw new Error('Input nesting limit');
    const token = this.take();
    if (token === '$') return { variable: this.take() };
    if (token === '[') { const items = []; while (this.peek() !== ']') items.push(this.value(depth + 1)); this.take(']'); return items; }
    if (token === '{') { const object: RecordData = {}; while (this.peek() !== '}') { const key = this.take(); this.take(':'); object[key] = this.value(depth + 1); } this.take('}'); return object; }
    if (token.startsWith('"""')) return token.slice(3, -3);
    if (token.startsWith('"')) return JSON.parse(token);
    if (token === 'true' || token === 'false') return token === 'true';
    if (token === 'null') return null;
    return /^-?\d/.test(token) ? Number(token) : token;
  }
  args(): RecordData {
    const result: RecordData = {};
    if (this.peek() !== '(') return result;
    this.take('(');
    while (this.peek() !== ')') { const key = this.take(); this.take(':'); result[key] = this.value(); }
    this.take(')'); return result;
  }
  directives(): { name: string; args: RecordData }[] {
    const result = [];
    while (this.peek() === '@') { this.take(); result.push({ name: this.take(), args: this.args() }); }
    return result;
  }
  fields(depth = 0): Selection[] {
    if (depth > 64) throw new Error('Selection nesting limit');
    this.take('{'); const fields: Selection[] = [];
    while (this.peek() !== '}') {
      if (this.peek() === '...') {
        this.take(); const name = this.take();
        const on = name === 'on' ? this.take() : undefined, directives = this.directives();
        fields.push({ name: '', alias: '', args: {}, directives, fields: on ? this.fields(depth + 1) : [], fragment: on ? undefined : name, on });
      } else {
        const first = this.take(); let name = first;
        if (this.peek() === ':') { this.take(); name = this.take(); }
        const args = this.args(), directives = this.directives();
        fields.push({ name, alias: first, args, directives, fields: this.peek() === '{' ? this.fields(depth + 1) : [] });
      }
    }
    this.take('}'); return fields;
  }
  parse(): Document {
    const operations: Operation[] = [], fragments = new Map();
    while (this.peek()) {
      if (this.peek() === 'fragment') { this.take(); const name = this.take(); this.take('on'); const on = this.take(); this.directives(); fragments.set(name, { on, fields: this.fields() }); continue; }
      let kind = 'query', name: string | undefined; const defaults: RecordData = {};
      if (this.peek() !== '{') {
        kind = this.take(); if (!['query', 'mutation', 'subscription'].includes(kind)) throw new Error(`Unknown operation ${kind}`);
        if (this.peek() !== '(' && this.peek() !== '{' && this.peek() !== '@') name = this.take();
        if (this.peek() === '(') {
          this.take();
          while (this.peek() !== ')') {
            this.take('$'); const key = this.take(); this.take(':');
            while (this.peek() && /^[\w\[\]!]$|^[_A-Za-z][_0-9A-Za-z]*$/.test(this.peek())) this.take();
            if (this.peek() === '=') { this.take(); defaults[key] = this.value(); }
          }
          this.take(')');
        }
        this.directives();
      }
      operations.push({ kind, name, defaults, fields: this.fields() });
    }
    if (!operations.length) throw new Error('No operation supplied');
    return { operations, fragments };
  }
}

// A compact schema drives both validation and introspection; resolvers never guess query text.
const schema: Record<string, Record<string, string>> = {
  Query: { viewer: 'User', repository: 'Repository', repositoryOwner: 'User', user: 'User', node: 'Node', nodes: '[Node]', search: 'SearchResultItemConnection', rateLimit: 'RateLimit', __type: '__Type', __schema: '__Schema' },
  Mutation: { createPullRequest: 'CreatePullRequestPayload', mergePullRequest: 'MergePullRequestPayload', createIssue: 'CreateIssuePayload', updateIssue: 'UpdateIssuePayload', closeIssue: 'UpdateIssuePayload', reopenIssue: 'UpdateIssuePayload', addComment: 'AddCommentPayload', createRepository: 'CreateRepositoryPayload', updatePullRequest: 'CreatePullRequestPayload' },
  Repository: { id: 'ID!', databaseId: 'Int', name: 'String!', nameWithOwner: 'String!', owner: 'User!', description: 'String', url: 'URI!', sshUrl: 'GitSSHRemote!', isPrivate: 'Boolean!', isFork: 'Boolean!', isArchived: 'Boolean!', defaultBranchRef: 'Ref', viewerPermission: 'RepositoryPermission', hasIssuesEnabled: 'Boolean!', hasWikiEnabled: 'Boolean!', hasProjectsEnabled: 'Boolean!', mergeCommitAllowed: 'Boolean!', squashMergeAllowed: 'Boolean!', rebaseMergeAllowed: 'Boolean!', deleteBranchOnMerge: 'Boolean!', parent: 'Repository', issue: 'Issue', issues: 'IssueConnection!', pullRequest: 'PullRequest', pullRequests: 'PullRequestConnection!', issueOrPullRequest: 'IssueOrPullRequest', labels: 'LabelConnection!', label: 'Label', refs: 'RefConnection!', ref: 'Ref', object: 'GitObject', createdAt: 'DateTime!', updatedAt: 'DateTime!', pushedAt: 'DateTime', diskUsage: 'Int', stargazerCount: 'Int!', forkCount: 'Int!', viewerHasStarred: 'Boolean!', assignableUsers: 'UserConnection!', collaborators: 'UserConnection!', milestones: 'MilestoneConnection!', discussionCategories: 'DiscussionCategoryConnection!' },
  User: { id: 'ID!', databaseId: 'Int', login: 'String!', name: 'String', url: 'URI!', avatarUrl: 'URI!', repositories: 'RepositoryConnection!', isViewer: 'Boolean!' },
  Issue: { id: 'ID!', databaseId: 'Int', number: 'Int!', title: 'String!', state: 'IssueState!', body: 'String!', bodyText: 'String!', bodyHTML: 'HTML!', url: 'URI!', author: 'User', createdAt: 'DateTime!', updatedAt: 'DateTime!', closedAt: 'DateTime', closed: 'Boolean!', repository: 'Repository!', labels: 'LabelConnection!', assignees: 'UserConnection!', comments: 'IssueCommentConnection!', milestone: 'Milestone', isPinned: 'Boolean!', locked: 'Boolean!', viewerDidAuthor: 'Boolean!', viewerCanUpdate: 'Boolean!', viewerCanReact: 'Boolean!', stateReason: 'IssueStateReason', projectItems: 'ProjectV2ItemConnection!', reactionGroups: '[ReactionGroup!]', participants: 'UserConnection!' },
  PullRequest: { id: 'ID!', databaseId: 'Int', number: 'Int!', title: 'String!', state: 'PullRequestState!', body: 'String!', bodyText: 'String!', bodyHTML: 'HTML!', url: 'URI!', headRefName: 'String!', baseRefName: 'String!', headRefOid: 'GitObjectID!', baseRefOid: 'GitObjectID!', headRef: 'Ref', baseRef: 'Ref', headRepository: 'Repository', baseRepository: 'Repository!', headRepositoryOwner: 'User', isCrossRepository: 'Boolean!', mergeable: 'MergeableState!', merged: 'Boolean!', mergedAt: 'DateTime', mergedBy: 'User', mergeCommit: 'Commit', isDraft: 'Boolean!', closed: 'Boolean!', closedAt: 'DateTime', author: 'User', createdAt: 'DateTime!', updatedAt: 'DateTime!', repository: 'Repository!', files: 'PullRequestChangedFileConnection!', changedFiles: 'Int!', additions: 'Int!', deletions: 'Int!', commits: 'PullRequestCommitConnection!', labels: 'LabelConnection!', assignees: 'UserConnection!', comments: 'IssueCommentConnection!', reviews: 'PullRequestReviewConnection!', latestReviews: 'PullRequestReviewConnection!', reviewRequests: 'ReviewRequestConnection!', reviewDecision: 'PullRequestReviewDecision', statusCheckRollup: 'StatusCheckRollup', potentialMergeCommit: 'Commit', maintainerCanModify: 'Boolean!', autoMergeRequest: 'AutoMergeRequest', mergeStateStatus: 'MergeStateStatus!', milestone: 'Milestone', viewerDidAuthor: 'Boolean!', viewerCanUpdate: 'Boolean!', viewerCanEnableAutoMerge: 'Boolean!', viewerCanDisableAutoMerge: 'Boolean!', projectItems: 'ProjectV2ItemConnection!', reactionGroups: '[ReactionGroup!]', participants: 'UserConnection!' },
  Ref: { id: 'ID!', name: 'String!', prefix: 'String!', target: 'Commit', repository: 'Repository!', branchProtectionRule: 'BranchProtectionRule', associatedPullRequests: 'PullRequestConnection!' },
  Commit: { id: 'ID!', oid: 'GitObjectID!', abbreviatedOid: 'String!', message: 'String!', messageHeadline: 'String!', messageBody: 'String!', committedDate: 'DateTime!', authoredDate: 'DateTime!', author: 'GitActor', committer: 'GitActor', url: 'URI!', tree: 'Tree!', parents: 'CommitConnection!', history: 'CommitConnection!', statusCheckRollup: 'StatusCheckRollup', checkSuites: 'CheckSuiteConnection!', signature: 'GitSignature', additions: 'Int!', deletions: 'Int!' },
  GitActor: { name: 'String', email: 'String', date: 'DateTime', user: 'User' },
  Tree: { id: 'ID!', oid: 'GitObjectID!', entries: '[TreeEntry!]' }, TreeEntry: { name: 'String!', path: 'String!', type: 'String!', mode: 'Int!', oid: 'GitObjectID!', object: 'GitObject' },
  Blob: { id: 'ID!', oid: 'GitObjectID!', text: 'String', byteSize: 'Int!', isBinary: 'Boolean!', isTruncated: 'Boolean!' },
  Label: { id: 'ID!', name: 'String!', color: 'String!', description: 'String', url: 'URI!', isDefault: 'Boolean!' },
  IssueComment: { id: 'ID!', databaseId: 'Int', body: 'String!', bodyText: 'String!', author: 'User', createdAt: 'DateTime!', updatedAt: 'DateTime!', url: 'URI!' },
  PullRequestChangedFile: { path: 'String!', additions: 'Int!', deletions: 'Int!', changeType: 'PatchStatus!' },
  PullRequestCommit: { commit: 'Commit!' },
  PageInfo: { hasNextPage: 'Boolean!', hasPreviousPage: 'Boolean!', startCursor: 'String', endCursor: 'String' },
  SearchResultItemConnection: { nodes: '[SearchResultItem]', edges: '[SearchResultItemEdge]', pageInfo: 'PageInfo!', issueCount: 'Int!', repositoryCount: 'Int!', userCount: 'Int!' },
  RateLimit: { cost: 'Int!', limit: 'Int!', remaining: 'Int!', resetAt: 'DateTime!', used: 'Int!' },
  CreatePullRequestPayload: { pullRequest: 'PullRequest', clientMutationId: 'String' }, MergePullRequestPayload: { pullRequest: 'PullRequest', clientMutationId: 'String' },
  CreateIssuePayload: { issue: 'Issue', clientMutationId: 'String' }, UpdateIssuePayload: { issue: 'Issue', clientMutationId: 'String' },
  AddCommentPayload: { commentEdge: 'IssueCommentEdge', subject: 'Issue', clientMutationId: 'String' }, CreateRepositoryPayload: { repository: 'Repository', clientMutationId: 'String' },
  __Schema: { queryType: '__Type!', mutationType: '__Type', subscriptionType: '__Type', types: '[__Type!]!', directives: '[__Directive!]!' },
  __Type: { kind: '__TypeKind!', name: 'String', description: 'String', fields: '[__Field!]', interfaces: '[__Type!]', possibleTypes: '[__Type!]', enumValues: '[__EnumValue!]', inputFields: '[__InputValue!]', ofType: '__Type', specifiedByURL: 'String' },
  __Field: { name: 'String!', description: 'String', args: '[__InputValue!]!', type: '__Type!', isDeprecated: 'Boolean!', deprecationReason: 'String' },
  __InputValue: { name: 'String!', description: 'String', type: '__Type!', defaultValue: 'String', isDeprecated: 'Boolean!', deprecationReason: 'String' },
  __EnumValue: { name: 'String!', description: 'String', isDeprecated: 'Boolean!', deprecationReason: 'String' },
  __Directive: { name: 'String!', description: 'String', locations: '[String!]!', args: '[__InputValue!]!', isRepeatable: 'Boolean!' },
};
const enums: Record<string, string[]> = { SearchType: ['ISSUE', 'REPOSITORY', 'USER', 'DISCUSSION'], IssueState: ['OPEN', 'CLOSED'], PullRequestState: ['OPEN', 'CLOSED', 'MERGED'], MergeableState: ['MERGEABLE', 'CONFLICTING', 'UNKNOWN'], RepositoryPermission: ['ADMIN', 'MAINTAIN', 'WRITE', 'TRIAGE', 'READ'], OrderDirection: ['ASC', 'DESC'], IssueOrderField: ['CREATED_AT', 'UPDATED_AT', 'COMMENTS'], PullRequestOrderField: ['CREATED_AT', 'UPDATED_AT'], MergeStateStatus: ['CLEAN', 'DIRTY', 'UNKNOWN', 'BLOCKED', 'BEHIND', 'UNSTABLE', 'HAS_HOOKS', 'DRAFT'], PullRequestMergeMethod: ['SQUASH', 'MERGE', 'REBASE'], PatchStatus: ['ADDED', 'MODIFIED', 'DELETED', 'RENAMED'], __TypeKind: ['SCALAR', 'OBJECT', 'INTERFACE', 'UNION', 'ENUM', 'INPUT_OBJECT', 'LIST', 'NON_NULL'] };
for (const node of ['Repository', 'Issue', 'PullRequest', 'Label', 'User', 'Ref', 'IssueComment', 'PullRequestChangedFile', 'PullRequestCommit', 'Commit', 'Milestone', 'DiscussionCategory', 'ProjectV2Item', 'PullRequestReview', 'ReviewRequest', 'CheckSuite']) {
  schema[`${node}Connection`] = { nodes: `[${node}]`, edges: `[${node}Edge]`, pageInfo: 'PageInfo!', totalCount: 'Int!' };
  schema[`${node}Edge`] = { node, cursor: 'String!' };
}
schema.SearchResultItemEdge = { node: 'SearchResultItem', cursor: 'String!' };
const unions: Record<string, string[]> = { Node: ['Repository', 'Issue', 'PullRequest', 'User', 'Ref', 'Commit', 'Blob'], IssueOrPullRequest: ['Issue', 'PullRequest'], SearchResultItem: ['Issue', 'PullRequest', 'Repository'], GitObject: ['Commit', 'Tree', 'Blob'] };
for (const name of ['Milestone', 'DiscussionCategory', 'ProjectV2Item', 'PullRequestReview', 'ReviewRequest', 'CheckSuite', 'ReactionGroup', 'StatusCheckRollup', 'AutoMergeRequest', 'BranchProtectionRule', 'GitSignature']) schema[name] ??= { id: 'ID', name: 'String', state: 'String', title: 'String' };
const baseType = (type: string) => type.replace(/[\[\]!]/g, '');
const fieldArgs: Record<string, Record<string, string>> = {
  'Query.repository': { owner: 'String!', name: 'String!' }, 'Query.__type': { name: 'String!' },
  'Repository.issues': { first: 'Int', last: 'Int', after: 'String', before: 'String', states: '[IssueState!]', orderBy: 'IssueOrder', filterBy: 'IssueFilters' },
  'Repository.pullRequests': { first: 'Int', last: 'Int', after: 'String', before: 'String', states: '[PullRequestState!]', headRefName: 'String', baseRefName: 'String', orderBy: 'IssueOrder' },
  'Repository.issue': { number: 'Int!' }, 'Repository.pullRequest': { number: 'Int!' }, 'Mutation.createPullRequest': { input: 'CreatePullRequestInput!' },
};
const inputs: Record<string, Record<string, string>> = {
  CreatePullRequestInput: { repositoryId: 'ID!', title: 'String!', body: 'String', headRefName: 'String!', baseRefName: 'String!', draft: 'Boolean', clientMutationId: 'String' },
  CreateIssueInput: { repositoryId: 'ID!', title: 'String!', body: 'String', labelIds: '[ID!]', clientMutationId: 'String' },
  IssueOrder: { field: 'IssueOrderField!', direction: 'OrderDirection!' }, IssueFilters: { states: '[IssueState!]', labels: '[String!]', createdBy: 'String' },
};
function typeInfo(type: string): RecordData {
  if (type.endsWith('!')) return { kind: 'NON_NULL', name: null, ofType: typeInfo(type.slice(0, -1)) };
  if (type.startsWith('[')) return { kind: 'LIST', name: null, ofType: typeInfo(type.slice(1, -1)) };
  return { kind: schema[type] ? 'OBJECT' : enums[type] ? 'ENUM' : inputs[type] ? 'INPUT_OBJECT' : unions[type] ? 'UNION' : 'SCALAR', name: type, description: `${type} type`, ofType: null };
}
function resolveValues(value: any, vars: RecordData): any {
  if (Array.isArray(value)) return value.map(v => resolveValues(v, vars));
  if (value && typeof value === 'object') {
    if (Object.keys(value).length === 1 && typeof value.variable === 'string') return vars[value.variable];
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, resolveValues(v, vars)]));
  }
  return value;
}
function tag(type: string, data: RecordData): RecordData { return { ...data, __typename: type }; }
function connection(nodes: RecordData[], args: RecordData): RecordData {
  let start = args.after ? Number(Buffer.from(args.after, 'base64').toString().split(':').pop()) + 1 : 0;
  let end = args.before ? Number(Buffer.from(args.before, 'base64').toString().split(':').pop()) : nodes.length;
  if (!Number.isFinite(start)) start = 0; if (!Number.isFinite(end)) end = nodes.length;
  start = Math.max(0, start); end = Math.min(nodes.length, end);
  if (args.last !== undefined) start = Math.max(start, end - Math.max(0, Math.min(Number(args.last), 1000)));
  else end = Math.min(end, start + Math.max(0, Math.min(Number(args.first ?? 100), 1000)));
  const cursor = (i: number) => Buffer.from(`cursor:${i}`).toString('base64');
  return { totalCount: nodes.length, issueCount: nodes.length, repositoryCount: nodes.length, userCount: nodes.length,
    nodes: nodes.slice(start, end), edges: nodes.slice(start, end).map((node, i) => ({ node, cursor: cursor(start + i) })),
    pageInfo: { hasNextPage: end < nodes.length, hasPreviousPage: start > 0, startCursor: start < end ? cursor(start) : null, endCursor: start < end ? cursor(end - 1) : null } };
}
class Executor {
  store: Store; document: Document; vars: RecordData; errors: { message: string }[] = []; budget = 100000;
  constructor(store: Store, document: Document, vars: RecordData) { this.store = store; this.document = document; this.vars = vars; }
  repo(r: Repository): RecordData { return tag('Repository', { _repo: r }); }
  item(r: Repository, value: RecordData): RecordData { return tag(value.head ? 'PullRequest' : 'Issue', { ...value, _repo: r }); }
  user(login?: string): RecordData { return tag('User', this.store.user(login)); }
  ref(r: Repository, name: string): RecordData | null {
    const sha = this.store.resolve(r, name); return sha ? tag('Ref', { _repo: r, id: `REF_${r.id}_${name}`, name: name.replace(/^refs\/heads\//, ''), prefix: 'refs/heads/', target: this.commit(r, sha), repository: this.repo(r), branchProtectionRule: null }) : null;
  }
  commit(r: Repository, sha: string): RecordData { return tag('Commit', { _repo: r, oid: sha }); }
  node(id: string): RecordData | null {
    for (const r of this.store.repos.values()) {
      if (id === `R_${r.id}`) return this.repo(r);
      for (const item of [...r.issues.values(), ...r.pulls.values()]) if (item.node_id === id) return this.item(r, item);
    }
    return id === 'U_1' ? this.user() : null;
  }
  expand(fields: Selection[], type: string, stack = new Set<string>()): Selection[] {
    return fields.flatMap(f => {
      const d = resolveValues(f.directives, this.vars);
      if (d.some((x: any) => x.name === 'skip' && x.args.if === true || x.name === 'include' && x.args.if === false)) return [];
      if (f.fragment) {
        if (stack.has(f.fragment)) throw new Error('Fragment cycle');
        const fragment = this.document.fragments.get(f.fragment);
        if (!fragment) throw new Error(`Unknown fragment ${f.fragment}`);
        if (fragment.on !== type && !(unions[fragment.on] ?? []).includes(type)) return [];
        return this.expand(fragment.fields, type, new Set([...stack, f.fragment]));
      }
      if (f.on) return f.on === type || (unions[f.on] ?? []).includes(type) ? this.expand(f.fields, type, stack) : [];
      return [f];
    });
  }
  validate(fields: Selection[], type: string, stack = new Set<string>()): void {
    for (const f of fields) {
      if (f.fragment) {
        if (stack.has(f.fragment)) throw new Error('Fragment cycle');
        const frag = this.document.fragments.get(f.fragment); if (!frag) throw new Error(`Unknown fragment ${f.fragment}`);
        this.validate(frag.fields, frag.on, new Set([...stack, f.fragment])); continue;
      }
      if (f.on) { this.validate(f.fields, f.on, stack); continue; }
      if (f.name === '__typename') continue;
      const target = schema[type]?.[f.name];
      if (!target) { this.errors.push({ message: `Cannot query field "${f.name}" on type "${type}".` }); continue; }
      if (f.fields.length) this.validate(f.fields, baseType(target), stack);
    }
  }
  project(value: any, type: string, fields: Selection[]): any {
    if (value == null) return null;
    if (Array.isArray(value)) return value.map(v => this.project(v, baseType(type), fields));
    if (!fields.length) return value;
    const actual = value.__typename ?? baseType(type), result: RecordData = {};
    for (const f of this.expand(fields, actual)) {
      if (--this.budget < 0) throw new Error('Query complexity limit');
      const args = resolveValues(f.args, this.vars);
      const target = f.name === '__typename' ? 'String' : schema[actual]?.[f.name] ?? 'String';
      const resolved = f.name === '__typename' ? actual : this.resolve(actual, value, f.name, args);
      result[f.alias] = this.project(resolved, target, f.fields);
    }
    return result;
  }
  resolve(type: string, p: RecordData, field: string, a: RecordData): any {
    const s = this.store, r = p._repo as Repository;
    if (type === 'Query') {
      if (field === '__type') return schema[a.name] || enums[a.name] || unions[a.name] || inputs[a.name] ? typeInfo(a.name) : null;
      if (field === '__schema') return { queryType: typeInfo('Query'), mutationType: typeInfo('Mutation'), subscriptionType: null, types: [...new Set([...Object.keys(schema), ...Object.keys(enums), ...Object.keys(inputs), ...Object.keys(unions), 'String', 'Int', 'Boolean', 'ID'])].map(typeInfo), directives: [] };
      if (field === 'viewer') return this.user();
      if (field === 'user' || field === 'repositoryOwner') return a.login === s.login ? this.user() : null;
      if (field === 'repository') { const repo = s.find(a.owner ?? s.login, a.name); return repo ? this.repo(repo) : null; }
      if (field === 'node') return this.node(a.id);
      if (field === 'nodes') return (a.ids ?? []).map((id: string) => this.node(id));
      if (field === 'rateLimit') return { cost: 1, limit: 500000, remaining: 499999, resetAt: '2100-01-01T00:00:00Z', used: 1 };
      if (field === 'search') {
        const q = a.query ?? '', match = /repo:([^\s/]+)\/([^\s]+)/.exec(q);
        const repos = match ? [s.find(match[1], match[2])].filter(Boolean) as Repository[] : [...s.repos.values()];
        let nodes = a.type === 'REPOSITORY' ? repos.map(r => this.repo(r)) : repos.flatMap(r => [...(q.includes('is:pr') ? [] : r.issues.values()), ...(q.includes('is:issue') ? [] : r.pulls.values())].map(v => this.item(r, v)));
        if (q.includes('is:open')) nodes = nodes.filter(i => i.state === 'open');
        if (q.includes('is:closed')) nodes = nodes.filter(i => i.state === 'closed');
        nodes.sort((a, b) => (b.number ?? 0) - (a.number ?? 0)); return connection(nodes, a);
      }
    }
    if (type === 'Mutation') return this.mutate(field, a.input ?? a);
    if (type === '__Type') {
      const name = p.name;
      if (field === 'fields') return schema[name] ? Object.entries(schema[name]).filter(([n]) => !n.startsWith('__')).map(([n, t]) => ({ name: n, description: n, type: typeInfo(t), args: Object.entries(fieldArgs[`${name}.${n}`] ?? {}).map(([name, type]) => ({ name, type: typeInfo(type), defaultValue: null })), isDeprecated: false, deprecationReason: null })) : null;
      if (field === 'enumValues') return enums[name]?.map(name => ({ name, description: name, isDeprecated: false, deprecationReason: null })) ?? null;
      if (field === 'inputFields') return inputs[name] ? Object.entries(inputs[name]).map(([name, t]) => ({ name, type: typeInfo(t), defaultValue: null })) : null;
      if (field === 'interfaces') return [];
      if (field === 'possibleTypes') return unions[name]?.map(typeInfo) ?? null;
    }
    if (type === 'Repository') {
      const json = s.repoJSON(r);
      const scalar: RecordData = { id: `R_${r.id}`, databaseId: r.id, name: r.name, nameWithOwner: json.full_name, owner: this.user(r.owner), description: r.description,
        url: json.html_url, sshUrl: json.ssh_url, isPrivate: r.private, isFork: false, isArchived: false, hasIssuesEnabled: true, hasWikiEnabled: false, hasProjectsEnabled: false,
        viewerPermission: 'ADMIN', defaultBranchRef: this.ref(r, r.defaultBranch), mergeCommitAllowed: true, squashMergeAllowed: true, rebaseMergeAllowed: false, deleteBranchOnMerge: false,
        createdAt: r.created, updatedAt: r.created, pushedAt: r.created, diskUsage: r.db.objects.size, stargazerCount: 0, forkCount: 0, viewerHasStarred: false };
      if (field in scalar) return scalar[field];
      if (field === 'issue' || field === 'pullRequest' || field === 'issueOrPullRequest') {
        const item = field === 'pullRequest' ? r.pulls.get(a.number) : r.issues.get(a.number) ?? (field === 'issueOrPullRequest' ? r.pulls.get(a.number) : undefined);
        return item ? this.item(r, item.head ? s.refreshPull(r, item) : item) : null;
      }
      if (field === 'issues' || field === 'pullRequests') {
        let items = [...(field === 'issues' ? r.issues : r.pulls).values()];
        const states = a.states ?? a.filterBy?.states;
        if (states?.length) items = items.filter(i => states.includes(i.merged ? 'MERGED' : i.state.toUpperCase()));
        if (a.headRefName) items = items.filter(i => i.head?.ref === a.headRefName);
        if (a.baseRefName) items = items.filter(i => i.base?.ref === a.baseRefName);
        const labels = a.labels ?? a.filterBy?.labels;
        if (labels?.length) items = items.filter(i => labels.every((name: string) => i.labels.some((l: any) => l.name === name)));
        const key = a.orderBy?.field === 'UPDATED_AT' ? 'updated_at' : 'created_at';
        const direction = a.orderBy?.direction === 'ASC' ? 1 : -1;
        items.sort((x, y) => direction * (String(x[key]).localeCompare(String(y[key])) || x.number - y.number));
        return connection(items.map(i => this.item(r, i.head ? s.refreshPull(r, i) : i)), a);
      }
      if (field === 'labels') return connection([...r.labels.values()].map(v => tag('Label', v)), a);
      if (field === 'label') { const v = r.labels.get(a.name); return v ? tag('Label', v) : null; }
      if (field === 'refs') return connection([...r.refs.keys()].filter(ref => ref.startsWith(a.refPrefix ?? 'refs/heads/')).map(ref => this.ref(r, ref)!), a);
      if (field === 'ref') return this.ref(r, a.qualifiedName);
      if (field === 'object') {
        const [ref, path] = String(a.expression ?? a.oid ?? r.defaultBranch).split(':');
        let sha = s.resolve(r, ref); if (!sha) return null;
        if (path) sha = r.db.files(sha).get(path)?.sha;
        if (!sha) return null;
        const o = r.db.get(sha);
        return o.type === 'commit' ? this.commit(r, sha) : tag(o.type === 'blob' ? 'Blob' : 'Tree', { _repo: r, oid: sha, id: `O_${sha}`, text: o.data.toString(), byteSize: o.data.length, isBinary: o.data.includes(0), isTruncated: false });
      }
      if (field === 'assignableUsers' || field === 'collaborators') return connection([this.user()], a);
    }
    if (type === 'User') {
      if (field === 'repositories') return connection([...s.repos.values()].map(r => this.repo(r)), a);
      if (field === 'id') return 'U_1'; if (field === 'databaseId') return 1; if (field === 'isViewer') return true;
      if (field === 'avatarUrl') return p.avatar_url;
    }
    if (type === 'Issue' || type === 'PullRequest') {
      const scalar: RecordData = { id: p.node_id, databaseId: p.id, state: p.merged ? 'MERGED' : p.state.toUpperCase(), bodyText: p.body, bodyHTML: p.body,
        url: p.html_url, author: this.user(), createdAt: p.created_at, updatedAt: p.updated_at, closedAt: p.closed_at, closed: p.state === 'closed',
        repository: this.repo(r), isPinned: false, locked: false, viewerDidAuthor: true, viewerCanUpdate: true, viewerCanReact: true, stateReason: p.state_reason?.toUpperCase() ?? null,
        headRefName: p.head?.ref, baseRefName: p.base?.ref, headRefOid: p.head?.sha, baseRefOid: p.base?.sha, headRepository: this.repo(r), baseRepository: this.repo(r),
        headRepositoryOwner: this.user(), isCrossRepository: false, mergeable: p.mergeable ? 'MERGEABLE' : 'CONFLICTING', mergedAt: p.merged_at, mergedBy: p.merged ? this.user() : null,
        isDraft: p.draft, maintainerCanModify: true, mergeStateStatus: p.merged ? 'CLEAN' : p.mergeable ? 'CLEAN' : 'DIRTY', reviewDecision: null, additions: 0, deletions: 0 };
      if (field in scalar) return scalar[field];
      if (field === 'labels') return connection(p.labels.map((v: any) => tag('Label', v)), a);
      if (field === 'comments') return connection((r.comments.get(p.number) ?? []).map(v => tag('IssueComment', v)), a);
      if (field === 'participants') return connection([this.user()], a);
      if (field === 'headRef' || field === 'baseRef') return this.ref(r, field === 'headRef' ? p.head.ref : p.base.ref);
      if (field === 'mergeCommit') return p.merge_commit_sha ? this.commit(r, p.merge_commit_sha) : null;
      if (field === 'files' || field === 'changedFiles') {
        const files = s.pullFiles(r, p); return field === 'changedFiles' ? files.length : connection(files.map(v => tag('PullRequestChangedFile', { path: v.filename, additions: 0, deletions: 0, changeType: v.status === 'added' ? 'ADDED' : v.status === 'removed' ? 'DELETED' : 'MODIFIED' })), a);
      }
      if (field === 'commits') {
        const nodes: RecordData[] = []; let sha = p.head.sha;
        while (sha && !r.db.isAncestor(sha, p.base.sha) && nodes.length < 1000) { nodes.unshift(tag('PullRequestCommit', { commit: this.commit(r, sha) })); sha = r.db.commit(sha).parents[0]; }
        return connection(nodes, a);
      }
    }
    if (type === 'Commit') {
      const c = r.db.commit(p.oid), json = s.commitJSON(r, p.oid), data = json.commit;
      const scalar: RecordData = { id: `C_${p.oid}`, abbreviatedOid: p.oid.slice(0, 7), message: data.message, messageHeadline: data.message.split('\n')[0], messageBody: data.message.split('\n').slice(1).join('\n'),
        committedDate: data.committer.date, authoredDate: data.author.date, author: { ...data.author, user: this.user() }, committer: { ...data.committer, user: this.user() },
        tree: tag('Tree', { _repo: r, oid: c.tree, id: `T_${c.tree}` }), url: json.html_url, additions: 0, deletions: 0 };
      if (field in scalar) return scalar[field];
      if (field === 'parents') return connection(c.parents.map(sha => this.commit(r, sha)), a);
      if (field === 'history') { const nodes = []; let sha = p.oid; while (sha && nodes.length < 10000) { nodes.push(this.commit(r, sha)); sha = r.db.commit(sha).parents[0]; } return connection(nodes, a); }
    }
    if (type === 'Tree' && field === 'entries') return [...r.db.tree(p.oid)].map(([name, e]) => ({ name, path: name, type: e.mode === '40000' ? 'tree' : 'blob', mode: parseInt(e.mode, 8), oid: e.sha, object: null }));
    if (type === 'Label' && field === 'id') return `L_${p.id}`;
    if (type === 'Label' && field === 'isDefault') return false;
    if (type === 'IssueComment') {
      const scalar: RecordData = { id: p.node_id, databaseId: p.id, bodyText: p.body, author: this.user(), createdAt: p.created_at, updatedAt: p.updated_at, url: p.html_url };
      if (field in scalar) return scalar[field];
    }
    if (field in p) return p[field];
    const target = schema[type]?.[field] ?? '';
    if (baseType(target).endsWith('Connection')) return connection([], a);
    if (target.startsWith('[')) return [];
    if (target.startsWith('Boolean')) return false;
    if (target.startsWith('Int')) return 0;
    return null;
  }
  mutate(field: string, input: RecordData): RecordData {
    const s = this.store, payload: RecordData = { clientMutationId: input.clientMutationId ?? null };
    if (field === 'createRepository') return { ...payload, repository: this.repo(s.createRepo({ ...input, private: input.visibility === 'PRIVATE' })) };
    if (field === 'createPullRequest') {
      const r = s.byId(input.repositoryId), p = s.createPull(r, { ...input, head: input.headRefName, base: input.baseRefName });
      return { ...payload, pullRequest: this.item(r, p) };
    }
    if (field === 'createIssue') {
      const r = s.byId(input.repositoryId), labels = (input.labelIds ?? []).map((id: string) => [...r.labels.values()].find(v => `L_${v.id}` === id)?.name ?? id);
      return { ...payload, issue: this.item(r, s.createIssue(r, { ...input, labels })) };
    }
    const node = this.node(input.pullRequestId ?? input.issueId ?? input.id ?? input.subjectId);
    if (!node) throw new HttpError(404, 'Could not resolve node');
    const r = node._repo as Repository, item = node.head ? r.pulls.get(node.number)! : r.issues.get(node.number)!;
    if (field === 'mergePullRequest') { s.mergePull(r, item.number, input); return { ...payload, pullRequest: this.item(r, item) }; }
    if (field === 'addComment') {
      const id = s.nextId(), comment = { id, node_id: `IC_${id}`, body: input.body, created_at: s.now(), updated_at: s.now(), user: s.user() };
      const comments = r.comments.get(item.number) ?? []; comments.push(comment); r.comments.set(item.number, comments); item.comments = comments.length;
      return { ...payload, commentEdge: { node: tag('IssueComment', comment) }, subject: this.item(r, item) };
    }
    if (field === 'closeIssue') item.state = 'closed';
    if (field === 'reopenIssue') item.state = 'open';
    if (input.state) item.state = String(input.state).toLowerCase();
    for (const k of ['title', 'body']) if (input[k] !== undefined) item[k] = input[k];
    item.updated_at = s.now();
    return { ...payload, [item.head ? 'pullRequest' : 'issue']: this.item(r, item) };
  }
}
export function executeGraphQL(store: Store, request: any): any {
  if (Array.isArray(request)) return request.map(item => executeGraphQL(store, item));
  try {
    if (!request || typeof request.query !== 'string') return { errors: [{ message: 'A GraphQL query is required' }] };
    const doc = new Parser(request.query).parse();
    const ops = request.operationName ? doc.operations.filter(op => op.name === request.operationName) : doc.operations;
    if (!ops.length) return { errors: [{ message: `Unknown operation ${request.operationName}` }] };
    const results = ops.map(op => {
      const executor = new Executor(store, doc, { ...op.defaults, ...(request.variables ?? {}) });
      const type = op.kind === 'mutation' ? 'Mutation' : 'Query';
      executor.validate(op.fields, type);
      if (executor.errors.length) return { errors: executor.errors };
      try { return { data: executor.project({}, type, op.fields) }; }
      catch (error) { return { data: null, errors: [{ message: error instanceof Error ? error.message : String(error) }] }; }
    });
    return results.length === 1 ? results[0] : results;
  } catch (error) { return { errors: [{ message: error instanceof Error ? error.message : String(error) }] }; }
}
export async function handleGraphQL(req: IncomingMessage, res: ServerResponse, store: Store): Promise<void> {
  sendJson(res, 200, executeGraphQL(store, await readJson(req)));
}
