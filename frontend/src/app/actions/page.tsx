"use client";

// Actions: coming soon. Placeholder route so the navigation is complete.

import { ListChecks } from "lucide-react";
import { PageHeader } from "@/components/Shell";
import { ButtonLink, Card, EmptyState } from "@/components/ui";

export default function ActionsPage() {
  return (
    <div>
      <PageHeader title="Actions" subtitle="Follow-ups the bid desk has to approve or carry out." />
      <Card>
        <EmptyState icon={<ListChecks size={16} strokeWidth={1.75} />} title="No actions yet"
          text="Open conflicts and failing cases are listed in the control room for now."
          action={<ButtonLink href="/" size="sm">Open the control room</ButtonLink>} />
      </Card>
    </div>
  );
}
