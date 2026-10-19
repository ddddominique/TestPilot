import { RunDetails } from "../../../components/run-details";

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <RunDetails key={id} id={id} />;
}
