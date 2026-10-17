"use client";

// Change impact: coming soon. Placeholder route so the navigation is complete.

import { GitCompareArrows } from "lucide-react";
import { PageHeader } from "@/components/Shell";
import { ButtonLink, Card, EmptyState } from "@/components/ui";

export default function ImpactPage() {
  return (
    <div>
      <PageHeader title="Change impact" subtitle="Which open cases and clauses a changed fact reaches." />
      <Card>
        <EmptyState icon={<GitCompareArrows size={16} strokeWidth={1.75} />} title="No change impact to show"
          text="Verdicts changed by a new document are listed in the control room for now."
          action={<ButtonLink href="/" size="sm">Open the control room</ButtonLink>} />
      </Card>
    </div>
  );
}
