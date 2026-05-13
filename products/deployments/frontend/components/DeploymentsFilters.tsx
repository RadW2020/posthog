import { useActions, useValues } from 'kea'

import { LemonInput, LemonSelect } from '@posthog/lemon-ui'

import { deploymentsLogic } from '../deploymentsLogic'
import { DeploymentStatus } from '../fixtures'

const STATUS_OPTIONS: { label: string; value: DeploymentStatus }[] = [
    { label: 'Ready', value: 'ready' },
    { label: 'Error', value: 'error' },
    { label: 'Building', value: 'building' },
    { label: 'Queued', value: 'queued' },
    { label: 'Initializing', value: 'initializing' },
    { label: 'Cancelled', value: 'cancelled' },
]

export function DeploymentsFilters(): JSX.Element {
    const { filters, authorOptions } = useValues(deploymentsLogic)
    const { setFilters } = useActions(deploymentsLogic)

    return (
        <div className="flex flex-wrap items-center gap-2">
            <LemonInput
                type="search"
                placeholder="Search by commit, branch, or id"
                value={filters.search}
                onChange={(search) => setFilters({ search, page: 1 })}
                className="min-w-64"
            />
            <LemonSelect
                mode="multiple"
                placeholder="Status"
                options={STATUS_OPTIONS}
                value={filters.status}
                onChange={(status) => setFilters({ status: (status ?? []) as DeploymentStatus[], page: 1 })}
                className="min-w-48"
            />
            <LemonSelect
                placeholder="Any author"
                options={[{ label: 'Any author', value: null as unknown as string }, ...authorOptions]}
                value={filters.author}
                onChange={(author) => setFilters({ author: author ?? null, page: 1 })}
                className="min-w-48"
                allowClear
            />
        </div>
    )
}
