export type DeploymentStatus = 'queued' | 'initializing' | 'building' | 'ready' | 'error' | 'cancelled'

export type DeploymentTriggerKind = 'git' | 'redeploy' | 'rollback' | 'seed'

export interface Deployment {
    id: string
    status: DeploymentStatus
    is_current: boolean
    created_at: string
    started_at: string | null
    finished_at: string | null
    duration_seconds: number | null
    commit_sha: string
    commit_message: string
    commit_author_name: string
    commit_author_email: string
    repo_url: string
    branch: string
    deployment_url: string
    preview_image_url: string
    trigger_kind: DeploymentTriggerKind
}

export interface DeploymentsFilters {
    search: string
    status: DeploymentStatus[]
    author: string | null
    order: string
    page: number
}

export const DEPLOYMENTS_PER_PAGE = 50

export const DEFAULT_DEPLOYMENT_FILTERS: DeploymentsFilters = {
    search: '',
    status: ['queued', 'initializing', 'building', 'ready', 'error'],
    author: null,
    order: '-created_at',
    page: 1,
}

export interface PaginatedDeployments {
    results: Deployment[]
    count: number
    offset: number
    filters: DeploymentsFilters
}

export function applyDeploymentFilters(rows: Deployment[], filters: DeploymentsFilters): PaginatedDeployments {
    let filtered = rows

    if (filters.status.length > 0) {
        const allowed = new Set(filters.status)
        filtered = filtered.filter((d) => allowed.has(d.status))
    }

    if (filters.author) {
        const author = filters.author.toLowerCase()
        filtered = filtered.filter(
            (d) => d.commit_author_email.toLowerCase() === author || d.commit_author_name.toLowerCase() === author
        )
    }

    if (filters.search.trim()) {
        const q = filters.search.trim().toLowerCase()
        filtered = filtered.filter(
            (d) =>
                d.commit_message.toLowerCase().includes(q) ||
                d.commit_sha.toLowerCase().includes(q) ||
                d.branch.toLowerCase().includes(q) ||
                d.id.toLowerCase().includes(q)
        )
    }

    const desc = filters.order.startsWith('-')
    const key = (desc ? filters.order.slice(1) : filters.order) as keyof Deployment
    filtered = [...filtered].sort((a, b) => {
        const av = a[key]
        const bv = b[key]
        if (av == null && bv == null) {
            return 0
        }
        if (av == null) {
            return desc ? 1 : -1
        }
        if (bv == null) {
            return desc ? -1 : 1
        }
        if (av < bv) {
            return desc ? 1 : -1
        }
        if (av > bv) {
            return desc ? -1 : 1
        }
        return 0
    })

    const count = filtered.length
    const offset = (filters.page - 1) * DEPLOYMENTS_PER_PAGE
    const results = filtered.slice(offset, offset + DEPLOYMENTS_PER_PAGE)

    return { results, count, offset, filters }
}

export function cloneForRedeploy(d: Deployment, newId: string, now: string): Deployment {
    return {
        ...d,
        id: newId,
        status: 'queued',
        is_current: false,
        created_at: now,
        started_at: null,
        finished_at: null,
        duration_seconds: null,
        preview_image_url: '',
        trigger_kind: 'redeploy',
    }
}

export function cloneForRollback(d: Deployment, newId: string, now: string): Deployment {
    return {
        ...d,
        id: newId,
        status: 'ready',
        is_current: true,
        created_at: now,
        started_at: now,
        finished_at: now,
        duration_seconds: 0,
        trigger_kind: 'rollback',
    }
}

export function formatDuration(seconds: number | null): string {
    if (seconds == null) {
        return '—'
    }
    if (seconds < 60) {
        return `${seconds}s`
    }
    const m = Math.floor(seconds / 60)
    const s = seconds % 60
    return s === 0 ? `${m}m` : `${m}m ${s}s`
}
